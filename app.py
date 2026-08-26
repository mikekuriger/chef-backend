### File: app.py CHEF v1
from authlib.integrations.flask_client import OAuth # for google auth
from datetime import datetime, date, timezone, timedelta
from dateutil.relativedelta import relativedelta
from enum import Enum
from flask_cors import CORS
from flask import abort, Blueprint, current_app, Flask, jsonify, redirect, render_template, render_template_string, request, session, url_for
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_mail import Message, Mail
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from jwt import InvalidTokenError
import jwt
from langdetect import detect
from openai import OpenAI
from PIL import Image
from PIL import ImageFilter
#from prompts import CATEGORY_PROMPTS, TONE_TO_STYLE
from prompts import CATEGORY_PROMPTS, RECIPE_COURSES, RECIPE_MAIN_INGREDIENTS
from quota import ensure_week_current, next_reset_iso, get_or_create_credits
from quota import decrement_text_or_deny, refund_text
from quota import decrement_image_or_deny, refund_image
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import JSON as MySQLJSON
from werkzeug.utils import secure_filename
from zoneinfo import ZoneInfo
import base64
import bcrypt
import hashlib, secrets, hmac
import io
import json
import logging
import math
import openai
import os
import re
import requests
import jwt
from jwt import PyJWKClient, InvalidTokenError
import shutil
import string
import threading
import time
import traceback
import uuid


logger = logging.getLogger()
# logger.setLevel(logging.INFO)
logger.setLevel(logging.DEBUG)

if os.getenv("LOG_TO_STDOUT", "1") == "1":
    handler = logging.StreamHandler()
else:
    log_dir = os.getenv("LOG_DIR", "/home/mk7193/chef")
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.FileHandler(os.path.join(log_dir, "chef.log"))

handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.addHandler(handler)


client = OpenAI()
openai.api_key = os.environ.get("OPENAI_API_KEY")

app = Flask(__name__)
# app.config.from_pyfile('config.py')
cfg_path = os.getenv("FLASK_CONFIG_FILE", "config.py")
if os.path.exists(cfg_path):
    app.config.from_pyfile(cfg_path)

# env overrides (Flask 3)
app.config.from_prefixed_env(prefix="CHEF")

CORS(app, supports_credentials=True,origins=["https://chef.zentha.me", "https://chef-us-west-01.zentha.me", "http://localhost:5173"])

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.init_app(app)
migrate = Migrate(app, db)
mail = Mail(app)

# for hashing email addresses after user deletes their account
# hashed email + any unused credits will be saved but all other data deleted
# new users will also be hashed and compared to list of hashes for a match
# if a match is found, user will get their credits restored
# this is to prevent abuse from users deleting and creating new accounts to get free credits
SECRET_PEPPER = "mikekuriger@gmail.com".encode("utf-8")
def hash_string_secret(value: str) -> str:
    return hmac.new(SECRET_PEPPER, value.encode("utf-8"), hashlib.sha256).hexdigest()

WEB_CLIENT_ID = "846080686597-bap701fbin0efm6m7lfcg3nhtc62nd6u.apps.googleusercontent.com"
IOS_CLIENT_ID = "846080686597-ubo1cq65c39mk4u5adc6r1vga65mqha8.apps.googleusercontent.com"
ALLOWED_AUDS = {WEB_CLIENT_ID, IOS_CLIENT_ID}
ALLOWED_ISS = {"https://accounts.google.com", "accounts.google.com"}

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
# For native Sign in with Apple, this should match the client_id used on iOS (bundle id or service id).
APPLE_BUNDLE_ID = "me.zentha.chef"
APPLE_CLIENT_ID = APPLE_BUNDLE_ID

_apple_jwk_client = PyJWKClient(APPLE_JWKS_URL)

# --- Admin config ---
# ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
app.config.setdefault("ADMIN_EMAILS", os.getenv("ADMIN_EMAILS", ""))


# Password reset with token (when user clicks the email)
# ---------------------------------------------------------------------------
# Minimal web reset page (temporary until mobile deep-link is live)
# ---------------------------------------------------------------------------
RESET_PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Reset your AI Chef password</title>
  <meta name="robots" content="noindex,nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { background:#0b0420; color:#fff; font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu; display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0; }
    .card { width: min(480px, 92vw); background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); border-radius: 14px; padding: 22px; box-shadow: 0 6px 24px rgba(0,0,0,0.35); }
    h1 { font-size: 20px; margin: 0 0 8px; }
    p { color:#D1B2FF; margin: 0 0 16px; }
    input[type=password] { width:100%; padding:12px; border-radius:10px; border:1px solid rgba(255,255,255,0.25); background:rgba(0,0,0,0.3); color:#fff; margin-bottom:12px; }
    button { width:100%; padding:12px; border-radius:10px; border:0; background:#fff; color:#000; font-weight:600; cursor:pointer; }
    .msg{ margin-top:12px; padding:10px; border-radius:10px; }
    .err{ background:rgba(255,0,0,0.16); border:1px solid rgba(255,0,0,0.35);}
    .ok{ background:rgba(0,200,0,0.16); border:1px solid rgba(0,200,0,0.35);}
    .hint{ font-size:12px; color:#bfb8d8; margin-top:8px; text-align:center;}
  </style>
</head>
<body>
  <div class="card">
    {% if invalid %}
      <h1>Link expired or invalid</h1>
      <p>Please request a new reset link from the AI Chef app.</p>
      <div class="hint">You can close this window.</div>
    {% elif done %}
      <h1>Password updated</h1>
      <p>You can now open the AI Chef app and sign in with your new password.</p>
      <div class="hint">You can close this window.</div>
    {% else %}
      <h1>Set a new password</h1>
      <p>Enter a new password for your account.</p>
      {% if error %}<div class="msg err">{{ error }}</div>{% endif %}
      <form method="post">
        <input type="archived" name="token" value="{{ token }}">
        <input type="password" name="pw1" placeholder="New password (min 8 chars)" minlength="8" required>
        <input type="password" name="pw2" placeholder="Confirm new password" minlength="8" required>
        <button type="submit">Update Password</button>
      </form>
      <div class="hint">After updating, open the AI Chef app and log in.</div>
    {% endif %}
  </div>
</body>
</html>
"""

# Confirm email with token
# ---------------------------------------------------------------------------
# Minimal web reset page (temporary until mobile deep-link is live)
# ---------------------------------------------------------------------------
CONFIRM_PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>AI Chef account confirmation</title>
  <meta name="robots" content="noindex,nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { background:#0b0420; color:#fff; font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu; display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0; }
    .card { width: min(520px, 92vw); background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); border-radius: 14px; padding: 22px; box-shadow: 0 6px 24px rgba(0,0,0,0.35); }
    h1 { font-size: 20px; margin: 0 0 8px; }
    p { color:#D1B2FF; margin: 0 0 16px; }
    .hint{ font-size:12px; color:#bfb8d8; margin-top:8px; text-align:center;}
  </style>
</head>
<body>
  <div class="card">
    {% if status == "ok" %}
      <h1>Account confirmed</h1>
      <p>You're all set! Open the AI Chef app and sign in.</p>
      <div class="hint">You can close this window.</div>
    {% elif status == "exists" %}
      <h1>Already confirmed</h1>
      <p>Your account was already active. Open the AI Chef app and sign in.</p>
      <div class="hint">You can close this window.</div>
    {% elif status == "expired" %}
      <h1>Link expired</h1>
      <p>Please request a new confirmation email from the AI Chef app.</p>
      <div class="hint">You can close this window.</div>
    {% else %}
      <h1>Invalid link</h1>
      <p>This confirmation link is not valid.</p>
      <div class="hint">You can close this window.</div>
    {% endif %}
  </div>
</body>
</html>
"""



# google auth
oauth = OAuth(app)

with open('google_oauth_credentials.json') as f:
    google_creds = json.load(f)

google = oauth.register(
    name='google',
    client_id=google_creds['web']['client_id'],
    client_secret=google_creds['web']['client_secret'],
    access_token_url='https://oauth2.googleapis.com/token',
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    api_base_url='https://www.googleapis.com/oauth2/v2/',
    client_kwargs={'scope': 'openid email profile'},
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration'
)

# Models
class User(db.Model, UserMixin):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=True, default='')
    first_name = db.Column(db.String(50), nullable=True)
    last_name = db.Column(db.String(50), nullable=True)
    signup_date = db.Column(db.DateTime, default=db.func.now())
    timezone = db.Column(db.String(50), nullable=True)  # e.g., "America/Los_Angeles"
    language = db.Column(db.String(10), nullable=True, default='en')
    email_confirmed = db.Column(db.Boolean, nullable=False, server_default=text("0"))
    apple_user_id = db.Column(db.String(255), unique=True, nullable=True, index=True)
    subscriptions = db.relationship("UserSubscription", back_populates="user")
    payments = db.relationship("PaymentTransaction", back_populates="user")


class Recipe(db.Model):
    __tablename__ = "recipe"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    text = db.Column(db.Text, nullable=True)
    ai_response = db.Column(db.Text, nullable=True)
    title = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)
    categories = db.Column(db.Text, nullable=True)
    tags = db.Column(db.Text, nullable=True)
    course = db.Column(db.String(50), nullable=True)
    main_ingredient = db.Column(db.String(100), nullable=True)
    ingredients_json = db.Column(db.Text, nullable=True)  # JSON list of {quantity, unit, name}
    time = db.Column(db.String(50))
    servings = db.Column(db.String(50))
    ingredients = db.Column(db.Text, nullable=True)
    instructions = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    variations = db.Column(db.Text, nullable=True)
    difficulty = db.Column(db.String(50))
    archived = db.Column(db.Boolean, default=False)
    image_file = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Attribution only — which shared canonical recipe this personal copy came
    # from, if any. Never used for diff/merge; editing this row never touches
    # the canonical row it points at.
    source_canonical_id = db.Column(db.Integer, db.ForeignKey('canonical_recipe.id'), nullable=True)


class CanonicalRecipe(db.Model):
    """
    A shared, de-duplicated recipe cache. Generic requests ("chicken parmesan")
    are matched here by embedding similarity before falling back to a fresh
    OpenAI call; a miss generates normally and gets inserted here too, so the
    next similar request is a hit. See find_canonical_match() / _is_generic_request().
    """
    __tablename__ = "canonical_recipe"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)
    course = db.Column(db.String(50), nullable=True)
    main_ingredient = db.Column(db.String(100), nullable=True)
    tags = db.Column(db.Text, nullable=True)
    time = db.Column(db.String(50))
    servings = db.Column(db.String(50), nullable=True)
    ingredients_json = db.Column(db.Text, nullable=True)
    instructions = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    variations = db.Column(db.Text, nullable=True)
    difficulty = db.Column(db.String(50))
    ai_response = db.Column(db.Text, nullable=True)
    image_file = db.Column(db.String(255), nullable=True)
    image_prompt = db.Column(db.Text, nullable=True)
    embedding = db.Column(db.Text, nullable=True)  # JSON-encoded list[float]
    hit_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)  # sha256 hex
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    request_ip = db.Column(db.String(64))
    user_agent = db.Column(db.String(256))
    user = db.relationship('User')


class EmailConfirmToken(db.Model):
    __tablename__ = 'email_confirm_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship('User')

# --- Subscription plans ---
class SubscriptionPlan(db.Model):
    __tablename__ = "subscription_plans"

    id = db.Column(db.String(50), primary_key=True)  # e.g., "pro_monthly"
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    period = db.Column(db.String(20), nullable=False)  # 'monthly', 'yearly'
    features = db.Column(MySQLJSON)
    product_id = db.Column(db.String(100))

    created_at = db.Column(db.DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    user_subscriptions = db.relationship("UserSubscription", back_populates="plan")

# --- User subscriptions ---
class UserSubscription(db.Model):
    __tablename__ = "user_subscriptions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan_id = db.Column(db.String(50), db.ForeignKey("subscription_plans.id"), nullable=False, index=True)

    status = db.Column(db.String(20), nullable=False)  # active/canceled/expired
    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime)

    auto_renew = db.Column(db.Boolean, server_default=text("0"), nullable=False)
    payment_method = db.Column(db.String(50))
    payment_provider = db.Column(db.String(50))
    provider_subscription_id = db.Column(db.String(100), index=True)
    provider_transaction_id = db.Column(db.String(100), index=True)
    receipt_data = db.Column(db.Text)

    created_at = db.Column(db.DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    user = db.relationship("User", back_populates="subscriptions")
    plan = db.relationship("SubscriptionPlan", back_populates="user_subscriptions")
    payments = db.relationship("PaymentTransaction", back_populates="subscription")

# --- Payment transactions ---
class PaymentTransaction(db.Model):
    __tablename__ = "payment_transactions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("user_subscriptions.id"), index=True)

    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(3), server_default=text("'USD'"), nullable=False)

    status = db.Column(db.String(20), nullable=False)   # pending/completed/failed
    provider = db.Column(db.String(50), nullable=False) # apple/google/stripe
    provider_transaction_id = db.Column(db.String(100), index=True)

    provider_response = db.Column(MySQLJSON)
    created_at = db.Column(db.DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False)

    user = db.relationship("User", back_populates="payments")
    subscription = db.relationship("UserSubscription", back_populates="payments")

# --- For free users ---
class UserCredits(db.Model):
    __tablename__ = "user_credits"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    text_remaining_week = db.Column(db.Integer, nullable=False, default=2)
    image_remaining_lifetime = db.Column(db.Integer, nullable=False, default=3)
    week_anchor_utc = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    user = db.relationship("User", backref=db.backref("credits", uselist=False))

# --- Subscription Service ---
class SubscriptionService:
    @staticmethod
    def get_user_subscription_status(user_id):
        """Get the current subscription status for a user"""
        # Find the most recent active subscription
        subscription = UserSubscription.query.filter_by(
            user_id=user_id,
            status='active'
        ).order_by(UserSubscription.end_date.desc()).first()

        if not subscription:
            # Return default free tier if no active subscription
            from quota import ensure_week_current, next_reset_iso  # safe import here
            uc = ensure_week_current(user_id)

            return {
                'tier': 'free',
                'expiry_date': None,
                'is_active': False,
                'auto_renew': False,
                'payment_method': None,
                'text_remaining_week': uc.text_remaining_week,
                'image_remaining_lifetime': uc.image_remaining_lifetime,
                'next_reset_iso': next_reset_iso(user_id)
            }

        # Get the plan details
        plan = subscription.plan

        return {
            'tier': plan.id,
            'expiry_date': subscription.end_date.isoformat() if subscription.end_date else None,
            'is_active': subscription.status == 'active',
            'auto_renew': subscription.auto_renew,
            'payment_method': subscription.payment_method
        }

    @staticmethod
    def get_subscription_plans():
        """Get all available subscription plans"""
        plans = SubscriptionPlan.query.all()
        return [{
            'id': plan.id,
            'name': plan.name,
            'description': plan.description,
            'price': float(plan.price),
            'period': plan.period,
            'features': plan.features,
            'product_id': plan.product_id
        } for plan in plans]

    @staticmethod
    def initiate_subscription(user_id, plan_id, payment_provider=None, receipt_data=None):
        """
        Initiate a subscription purchase

        Args:
            user_id: The user ID
            plan_id: The subscription plan ID
            payment_provider: The payment provider (apple/google/stripe)
            receipt_data: Receipt data for app store purchases

        Returns:
            Dictionary with subscription details or payment URL
        """
        # Allow lookup by primary key or by product_id (store product identifier)
        plan = SubscriptionPlan.query.get(plan_id)
        if not plan:
            plan = SubscriptionPlan.query.filter_by(product_id=plan_id).first()
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")

        # Handle different payment providers
        if payment_provider in ('apple', 'google'):
            # Verify receipt with app store/google play
            if not receipt_data:
                raise ValueError("Receipt data required for app store/google play purchases")

            # Verify receipt (implementation depends on provider)
            if payment_provider == 'apple':
                verification_result = SubscriptionService._verify_apple_receipt(receipt_data)
            else:  # google
                verification_result = SubscriptionService._verify_google_receipt(receipt_data)

            if not verification_result.get('valid'):
                raise ValueError(f"Invalid receipt: {verification_result.get('message')}")

            # Create subscription record
            subscription = SubscriptionService._create_subscription(
                user_id=user_id,
                plan_id=plan_id,
                payment_provider=payment_provider,
                provider_subscription_id=verification_result.get('subscription_id'),
                provider_transaction_id=verification_result.get('transaction_id'),
                receipt_data=receipt_data,
                auto_renew=True
            )

            # Create payment record
            SubscriptionService._create_payment(
                user_id=user_id,
                subscription_id=subscription.id,
                amount=float(plan.price),
                provider=payment_provider,
                provider_transaction_id=verification_result.get('transaction_id'),
                provider_response=verification_result
            )

            return {'success': True}

        elif payment_provider == 'stripe':
            # For web payments, create a Stripe checkout session
            # This is a placeholder - you would integrate with Stripe API here
            payment_url = f"https://example.com/checkout?plan={plan_id}&user={user_id}"
            return {'payment_url': payment_url}

        else:
            # Default web payment flow (customize based on your payment processor)
            payment_url = f"https://example.com/checkout?plan={plan_id}&user={user_id}"
            return {'payment_url': payment_url}

    @staticmethod
    def cancel_subscription(user_id):
        """Cancel a user's subscription"""
        subscription = UserSubscription.query.filter_by(
            user_id=user_id,
            status='active'
        ).order_by(UserSubscription.end_date.desc()).first()

        if not subscription:
            return False

        # Update subscription status
        subscription.status = 'canceled'
        subscription.auto_renew = False
        db.session.commit()

        # If using a payment provider, you might need to cancel with them too
        if subscription.payment_provider in ('apple', 'google', 'stripe'):
            # This would be implemented based on the provider's API
            pass

        return True

    @staticmethod
    def update_payment_method(user_id, payment_details):
        """Update a user's payment method"""
        subscription = UserSubscription.query.filter_by(
            user_id=user_id,
            status='active'
        ).order_by(UserSubscription.end_date.desc()).first()

        if not subscription:
            return False

        # Update payment method
        subscription.payment_method = payment_details.get('method')
        db.session.commit()

        # If using a payment provider, you might need to update with them too
        if subscription.payment_provider in ('apple', 'google', 'stripe'):
            # This would be implemented based on the provider's API
            pass

        return True

    @staticmethod
    def _create_subscription(
        user_id,
        plan_id,
        payment_provider=None,
        provider_subscription_id=None,
        provider_transaction_id=None,
        receipt_data=None,
        auto_renew=True,
    ):
        plan = SubscriptionPlan.query.get(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found in _create_subscription")

        now = datetime.utcnow()

        # Simple end_date calculation – keep whatever logic you already have
        if plan.period == "monthly":
            end_date = now + relativedelta(months=1)
        elif plan.period == "yearly":
            end_date = now + relativedelta(years=1)
        else:
            # fallback, or raise
            end_date = now + relativedelta(months=1)

        # First, see if we already have this exact provider+transaction
        existing = None
        if payment_provider and provider_transaction_id:
            existing = UserSubscription.query.filter_by(
                payment_provider=payment_provider,
                provider_transaction_id=provider_transaction_id,
            ).first()

        if existing:
            # Idempotent update of an existing subscription row
            logger.info(
                "SubscriptionService._create_subscription: updating existing "
                f"user_sub id={existing.id} user={user_id} provider={payment_provider} "
                f"txn={provider_transaction_id}"
            )
            existing.plan_id = plan_id
            existing.status = "active"
            existing.start_date = now
            existing.end_date = end_date
            existing.auto_renew = auto_renew
            existing.payment_method = payment_provider
            existing.provider_subscription_id = provider_subscription_id
            existing.receipt_data = receipt_data
            db.session.commit()
            return existing

        # Otherwise, create a new subscription row
        subscription = UserSubscription(
            user_id=user_id,
            plan_id=plan_id,
            status="active",
            start_date=now,
            end_date=end_date,
            auto_renew=auto_renew,
            payment_method=payment_provider,
            payment_provider=payment_provider,
            provider_subscription_id=provider_subscription_id,
            provider_transaction_id=provider_transaction_id,
            receipt_data=receipt_data,
        )

        try:
            db.session.add(subscription)
            db.session.commit()
            logger.info(
                "SubscriptionService._create_subscription: created new "
                f"user_sub id={subscription.id} user={user_id} provider={payment_provider} "
                f"txn={provider_transaction_id}"
            )
            return subscription
        except IntegrityError as e:
            db.session.rollback()
            logger.warning(
                "SubscriptionService._create_subscription: IntegrityError on insert, "
                "retrying as update: %s",
                e,
            )
            # Last-resort: fetch again and update, in case of race
            if payment_provider and provider_transaction_id:
                existing = UserSubscription.query.filter_by(
                    payment_provider=payment_provider,
                    provider_transaction_id=provider_transaction_id,
                ).first()
                if existing:
                    existing.plan_id = plan_id
                    existing.status = "active"
                    existing.start_date = now
                    existing.end_date = end_date
                    existing.auto_renew = auto_renew
                    existing.payment_method = payment_provider
                    existing.provider_subscription_id = provider_subscription_id
                    existing.receipt_data = receipt_data
                    db.session.commit()
                    return existing

            # If we still can't find it, re-raise so you see the error
            raise

        return {'valid': False, 'message': f"Invalid receipt: {result.get('status')}"}


    @staticmethod
    def _verify_apple_receipt(receipt_data: str) -> dict:
        """
        Verify an Apple purchase.

        1. First, try to treat receipt_data as StoreKit 2 transaction JSON
           (what your Flutter app is actually sending now).
        2. If that fails, fall back to legacy /verifyReceipt flow which expects
           base64 app receipt.
        """

        # --- Path 1: StoreKit 2 transaction JSON from the client ---
        try:
            tx = json.loads(receipt_data)
            if isinstance(tx, dict) and "transactionId" in tx:
                logger.info("Apple verify: treating receipt_data as StoreKit2 JSON transaction")

                transaction_id = tx.get("transactionId")
                original_transaction_id = (
                    tx.get("originalTransactionId") or transaction_id
                )

                # expiresDate is in ms since epoch, optional
                expires_ms = tx.get("expiresDate")
                expiry_iso = None
                if expires_ms:
                    try:
                        ms = int(expires_ms)
                        expiry_iso = datetime.utcfromtimestamp(ms / 1000.0).isoformat() + "Z"
                    except Exception as e:
                        logger.warning(f"Could not parse expiresDate={expires_ms}: {e}")

                return {
                    "valid": True,
                    "subscription_id": original_transaction_id,
                    "transaction_id": transaction_id,
                    "expiry_date": expiry_iso,
                    "raw": tx,
                }
        except Exception as e:
            # Not JSON or not the expected shape – fall back to legacy logic
            logger.info(f"Apple verify: receipt_data is not StoreKit2 JSON: {e}")

        # --- Path 2: legacy /verifyReceipt base64 receipt (only if above fails) ---

        shared_secret = SubscriptionService.APPSTORE_SHARED_SECRET
        if not shared_secret:
            logger.error("APPSTORE_SHARED_SECRET is not configured")
            return {"valid": False, "message": "Missing shared secret"}

        def call_apple(url: str) -> dict:
            payload = {
                "receipt-data": receipt_data,
                "password": shared_secret,
                "exclude-old-transactions": True,
            }
            r = requests.post(url, json=payload, timeout=10)
            try:
                return r.json()
            except Exception:
                logger.error(f"Apple verifyReceipt non-JSON response: {r.text}")
                return {"status": -1, "message": "non-JSON response"}

        prod_url = "https://buy.itunes.apple.com/verifyReceipt"
        sandbox_url = "https://sandbox.itunes.apple.com/verifyReceipt"

        result = call_apple(prod_url)
        status = result.get("status")

        if status == 21007:
            logger.info("Apple verifyReceipt 21007 → retrying sandbox endpoint")
            result = call_apple(sandbox_url)
            status = result.get("status")

        if status != 0:
            return {
                "valid": False,
                "message": f"Apple verifyReceipt status={status}",
            }

        latest_info = None
        info_list = result.get("latest_receipt_info")
        if isinstance(info_list, list) and info_list:
            latest_info = info_list[-1]
        elif isinstance(info_list, dict):
            latest_info = info_list

        if not latest_info:
            return {
                "valid": False,
                "message": "Apple verifyReceipt: missing latest_receipt_info",
            }

        sub_id = (
            latest_info.get("original_transaction_id")
            or latest_info.get("transaction_id")
        )
        txn_id = latest_info.get("transaction_id")

        expires_ms = latest_info.get("expires_date_ms")
        expiry_iso = None
        if expires_ms:
            try:
                ms = int(expires_ms)
                expiry_iso = datetime.utcfromtimestamp(ms / 1000.0).isoformat() + "Z"
            except Exception as e:
                logger.warning(f"Could not parse expires_date_ms={expires_ms}: {e}")

        return {
            "valid": True,
            "subscription_id": sub_id,
            "transaction_id": txn_id,
            "expiry_date": expiry_iso,
            "raw": latest_info,
        }
    

    # Android
    def _get_android_publisher_client():
        creds = service_account.Credentials.from_service_account_file(
            app.config["GOOGLE_SERVICE_ACCOUNT_JSON"],
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _verify_google_receipt(receipt_data: str) -> dict:
        """
        Verify a Google Play subscription using purchases.subscriptionsv2.get.
        receipt_data: purchase token from the device (serverVerificationData on Android).
        Returns {valid: bool, subscription_id, transaction_id, product_id, expiry_date?, message?}
        """

        try:
            android_publisher = _get_android_publisher_client()
            package_name = app.config["GOOGLE_PACKAGE_NAME"]

            # v2 endpoint – recommended for modern subs.:contentReference[oaicite:5]{index=5}
            resp = (
                android_publisher
                .purchases()
                .subscriptionsv2()
                .get(
                    packageName=package_name,
                    token=receipt_data,
                )
                .execute()
            )

            # SubscriptionPurchaseV2 structure:
            # - subscriptionState: "SUBSCRIPTION_STATE_ACTIVE", etc.
            # - latestOrderId: the most recent order ID
            # - lineItems[0].productId, lineItems[0].expiryTime, etc.:contentReference[oaicite:6]{index=6}
            state = resp.get("subscriptionState")
            line_items = resp.get("lineItems") or []
            line = line_items[0] if line_items else {}
            product_id = line.get("productId")
            expiry_time = line.get("expiryTime")  # e.g. "2025-01-15T10:00:00Z"

            if state != "SUBSCRIPTION_STATE_ACTIVE":
                return {
                    "valid": False,
                    "product_id": product_id,
                    "message": f"Google subscriptionState={state}",
                }

            latest_order_id = resp.get("latestOrderId")

            return {
                "valid": True,
                "subscription_id": latest_order_id,
                "transaction_id": latest_order_id,
                "product_id": product_id,
                "expiry_date": expiry_time,
            }

        except HttpError as e:
            return {
                "valid": False,
                "message": f"Google API error: {e}",
            }
        except Exception as e:
            return {
                "valid": False,
                "message": f"Unexpected Google verification error: {e}",
            }


    def process_subscription_renewals():
        """
        Process subscription renewals and expirations
        """
        # Get all active subscriptions that are due for renewal
        subscriptions = UserSubscription.query.filter(
            UserSubscription.status == 'active',
            UserSubscription.end_date <= datetime.utcnow() + timedelta(days=1),
            UserSubscription.auto_renew == True
        ).all()
        for subscription in subscriptions:
            # Process renewal based on payment provider
            if subscription.payment_provider == 'apple':
                # Verify subscription status with Apple
                pass
            elif subscription.payment_provider == 'google':
                # Verify subscription status with Google
                pass
            elif subscription.payment_provider == 'stripe':
                # Process renewal with Stripe
                pass

# Notes
NOTES_MAX_LEN = 8000
NOTES_AI_ENABLED = False
REANALYZE_WITH_NOTES_ALLOWED = False
NOTES_POLICY_VERSION = "v1"

# pro
def _user_is_pro(user_id: int) -> bool:
    try:
        st = SubscriptionService.get_user_subscription_status(user_id)
        tier = (st.get("tier") or "").lower()
        active = st.get("is_active") is True
        return active and (tier.startswith("pro") or tier.startswith("trial"))
    except Exception:
        return False

from functools import wraps
from flask import jsonify

def requires_pro(fn):
    @wraps(fn)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated or not _user_is_pro(current_user.id):
            return jsonify({"error": "pro_required"}), 402
        return fn(*args, **kwargs)
    return _wrap

def _iso_utc(dt: datetime | None) -> str | None:
    if not dt:
        return None
    # store naive UTC in DB; emit ISO with Z for API consistency
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

RESET_TTL_MINUTES = 60  # tweak as you like

def _generate_raw_token() -> str:
    return secrets.token_urlsafe(32)

def _password_policy_ok(pw: str) -> bool:
    return isinstance(pw, str) and len(pw) >= 8  # expand if needed


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Sends confirmation email (new)
def send_confirmation_email(recipient_email, token):
    base = app.config.get("CONFIRM_LINK_BASE", "https://chef-us-west-01.zentha.me/confirm")
    confirm_url = f"{base}?token={token}"
    msg = Message(
        subject="Confirm your AI Chef account",
        recipients=[recipient_email],
        body=(
            "Welcome to AI Chef!\n\n"
            "Click the link below to confirm your account:\n\n"
            f"{confirm_url}\n\n"
            "After confirming, open the AI Chef app and sign in.\n"
            "If you didn't sign up for AI Chef, ignore this message."
        )
    )
    mail.send(msg)


def _get_admin_emails():
    cfg = (current_app.config.get("ADMIN_EMAILS")
           or os.getenv("ADMIN_EMAILS")
           or "")
    # allow comma or semicolon
    parts = cfg.replace(";", ",").split(",")
    return {p.strip().lower() for p in parts if p.strip()}

def is_admin_user():
    return current_user.is_authenticated and (current_user.email or "").lower() in _get_admin_emails()

def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    @login_required
    def _wrap(*a, **k):
        if not is_admin_user():
            abort(403)
        return fn(*a, **k)
    return _wrap


# ROUTES

# profile update page
@app.route('/api/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = current_user

    if request.method == 'GET':
        return jsonify({
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'timezone': user.timezone,
        })

    # POST: update profile
    data = request.form
    file = request.files.get('avatar')

    if 'email' in data:
        user.email = data['email']
    if 'first_name' in data:
        user.first_name = data['first_name']
    if 'last_name' in data:
        user.last_name = data['last_name']
    db.session.commit()
    return jsonify({
      'first_name': user.first_name
  })


# for google logins via web app
# @app.route('/login/google')
# def login_google():
#     redirect_uri = url_for('auth_google', _external=True)
#     return google.authorize_redirect(redirect_uri)

# for google logins via web app
@app.route('/auth/google')
def auth_google():
    token = google.authorize_access_token()
    resp = google.get('userinfo')
    user_info = resp.json()

    email = user_info['email']
    full_name = user_info.get("name", "")
    name = full_name.split()[0] if full_name else ""

    # Check if user exists
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(
            email=email,
            first_name=name or "Unknown",
            last_name=last_name or "Unknown",
            password='',
            email_confirmed=True,
        )
        db.session.add(user)
        db.session.commit()
    elif not user.email_confirmed:
        user.email_confirmed = True
        db.session.commit()

    # login_user(user)
    login_user(user, remember=True, duration=timedelta(days=90))
    # return redirect("/dashboard?confirmed=1")
    return redirect("/dashboard")



# for google logins via mobile app
@app.route('/api/google_login', methods=['POST'])
def api_google_login():
    data = request.get_json(silent=True) or {}
    token = data.get('id_token')
    if not token:
        return jsonify({"error": "missing id_token"}), 400

    try:
        req = google_requests.Request()
        idinfo = id_token.verify_oauth2_token(token, req, audience=None)

        aud = idinfo.get("aud")
        azp = idinfo.get("azp")
        iss = idinfo.get("iss")
        email = (idinfo.get("email") or "").strip().lower()
        email_verified = idinfo.get("email_verified", False)
        full_name = idinfo.get("name") or ""
        first_name = full_name.split()[0] if full_name else "Unknown"

        if iss not in ALLOWED_ISS:
            raise ValueError(f"bad iss: {iss}")

        if aud not in ALLOWED_AUDS:
            if azp not in ALLOWED_AUDS:
                raise ValueError(f"bad aud: {aud} azp: {azp}")

        if not email or not email_verified:
            raise ValueError("email not verified")

        # ------------ NORMAL / REACTIVATION FLOW ------------

        # 1) Try real email first (case-insensitive)
        user = User.query.filter(func.lower(User.email) == email).first()

        if not user:
            # 2) Try deleted/reactivated user: email stored as hash
            email_hash = hash_string_secret(email)
            deleted = User.query.filter(User.email == email_hash).first()

            if deleted:
                # Reactivate this user in place, keep same id & credits
                deleted.email = email
                deleted.first_name = first_name or deleted.first_name
                deleted.email_confirmed = True
                # Optionally clear deletion flags if you have them:
                # deleted.deleted_at = None
                # deleted.status = "active"

                db.session.commit()
                user = deleted
            else:
                # 3) Truly new user – create fresh
                user = User(
                    email=email,
                    first_name=first_name,
                    password='',
                    timezone='',
                    email_confirmed=True,
                )
                db.session.add(user)
                db.session.commit()

        else:
            # Existing normal user: ensure confirmed
            if not user.email_confirmed:
                user.email_confirmed = True
                db.session.commit()

        # Log them in (whether new, existing, or reactivated)
        login_user(user)
        return jsonify({"success": True})

    except Exception as e:
        try:
            logger.info(
                "google login failed: aud=%s azp=%s iss=%s email=%s err=%s",
                idinfo.get("aud") if 'idinfo' in locals() else None,
                idinfo.get("azp") if 'idinfo' in locals() else None,
                idinfo.get("iss") if 'idinfo' in locals() else None,
                idinfo.get("email") if 'idinfo' in locals() else None,
                e,
            )
        except Exception:
            logger.info("google login failed: %s", e)
        return jsonify({"error": "Invalid token, naughty!"}), 400



# helper for Apple verification
def verify_apple_identity_token(identity_token: str) -> dict:
    """Verify an Apple Sign in with Apple identity token (JWT) and return its claims."""
    if not APPLE_CLIENT_ID:
        raise RuntimeError(
            "APPLE_CLIENT_ID or APPLE_BUNDLE_ID must be configured on the server"
        )

    try:
        # Get the signing key for this token from Apple's JWKS
        signing_key = _apple_jwk_client.get_signing_key_from_jwt(identity_token)
    except Exception as e:
        logger.exception("Failed to get Apple signing key from JWKS: %s", e)
        raise

    try:
        claims = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=APPLE_CLIENT_ID,
            issuer=APPLE_ISSUER,
        )
        # Log the good case at info level once while testing
        # logger.info(
        #     "Apple token verified: iss=%s aud=%s sub=%s email=%s exp=%s",
        #     claims.get("iss"),
        #     claims.get("aud"),
        #     claims.get("sub"),
        #     claims.get("email"),
        #     claims.get("exp"),
        # )
        return claims

    except InvalidTokenError as e:
        # Try decoding without verification just so we can inspect claims.
        try:
            raw_claims = jwt.decode(
                identity_token,
                options={
                    "verify_signature": False,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_exp": False,
                },
            )
            # logger.warning(
            #     "Apple token failed verification (%s). Raw claims: iss=%s aud=%s sub=%s email=%s exp=%s",
            #     e,
            #     raw_claims.get("iss"),
            #     raw_claims.get("aud"),
            #     raw_claims.get("sub"),
            #     raw_claims.get("email"),
            #     raw_claims.get("exp"),
            # )
        except Exception as inner:
            logger.warning(
                "Apple token failed verification (%s) and could not decode raw claims: %s",
                e,
                inner,
            )
        raise

    except Exception as e:
        logger.exception("Unexpected error verifying Apple identity token: %s", e)
        raise



# Apple Logins on IOS
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

@app.route("/api/apple_login", methods=["POST"])
def apple_login():
    data = request.get_json(silent=True) or {}
    identity_token = data.get("identity_token")
    authorization_code = data.get("authorization_code")  # currently unused
    user_identifier = data.get("user_identifier")
    email = data.get("email")
    full_name = data.get("full_name")
    first_name = full_name.split()[0] if full_name else ""

    if not identity_token:
        return jsonify({"error": "Missing identity token"}), 400

    # Verify the Apple identity token (signature + iss/aud/exp).
    try:
        claims = verify_apple_identity_token(identity_token)
    except (InvalidTokenError, RuntimeError) as e:
        logger.info("Apple login failed token check: %s", e)
        return jsonify({"error": "Invalid Apple identity token"}), 400
    except Exception:
        logger.exception("Apple login unexpected error while verifying token")
        return jsonify({"error": "Apple identity token verification failed"}), 400

    token_sub = claims.get("sub")
    if not token_sub:
        return jsonify({"error": "Apple identity token missing subject"}), 400

    # If the client also sent a user_identifier, make sure it matches the token.
    if user_identifier and user_identifier != token_sub:
        return jsonify({"error": "Apple user id mismatch"}), 400

    apple_user_id = token_sub

    email_from_token = claims.get("email")
    email_verified = claims.get("email_verified")
    if isinstance(email_verified, str):
        email_verified = email_verified.lower() == "true"

    # Prefer the (verified) email from the token, but fall back to payload.
    if email_from_token and (email_verified is True or email_verified is None):
        email = email_from_token or email

    # Normalize email
    email = (email or "").strip().lower()

    # ---- USER LOOKUP / REACTIVATION FLOW ----

    user = None

    # 1) Try by apple_user_id first
    if apple_user_id:
        user = User.query.filter_by(apple_user_id=apple_user_id).first()

        # If this user has a hashed email matching current email, restore it
        if user and email:
            email_hash = hash_string_secret(email)
            if user.email == email_hash:
                user.email = email
                user.first_name = first_name or user.first_name
                user.email_confirmed = True

    # 2) If no user yet, try by real email (case-insensitive)
    if not user and email:
        user = User.query.filter(func.lower(User.email) == email).first()

    # 3) If still no user, try “deleted” user where email == hash(email)
    if not user and email:
        email_hash = hash_string_secret(email)
        deleted = User.query.filter(User.email == email_hash).first()
        if deleted:
            logger.info("Reactivating deleted user via Apple login: %s", email)
            if first_name:
                deleted.first_name = first_name
            else:
                if deleted.first_name.startswith("Deleted-"):
                    deleted.first_name = deleted.first_name[len("Deleted-"):]
            deleted.email = email
            deleted.email_confirmed = True
            if not deleted.apple_user_id:
                deleted.apple_user_id = apple_user_id
            user = deleted

    # 4) If still no user, create new one
    if not user:
        if not email:
            # Apple didn’t provide an email we can trust; we can’t create an account.
            return jsonify({
                "error": (
                    "Apple did not provide an email address, try clearing your "
                    "saved password for AI Chef in Settings / your account / "
                    "Sign in with Apple, then try again."
                )
            }), 400

        user = User(
            apple_user_id=apple_user_id,
            email=email,
            first_name=first_name,
            password='',
            timezone='',
            email_confirmed=True,
        )
        db.session.add(user)
    else:
        # If we found user by email, make sure apple_user_id is attached
        if apple_user_id and not user.apple_user_id:
            user.apple_user_id = apple_user_id
        # Make sure they’re marked confirmed
        if not user.email_confirmed:
            user.email_confirmed = True

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Account conflict, please contact support"}), 400

    # 5) Log the user in
    login_user(user)
    return jsonify({"success": True}), 200


# New user registration
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    logger.info(f"📨 Registration attempt: {email}")

    EMAIL_REGEX = re.compile(r"^[^@]+@[^@]+\.[^@]+$")

    if not first_name or len(first_name) > 50:
        logger.warning("❌ Invalid name")
        return jsonify({"error": "Name must be 1–50 characters"}), 400

    if not password or len(password) < 8:
        logger.warning("❌ Invalid password")
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    if not email or not EMAIL_REGEX.match(email):
        logger.warning("❌ Invalid email")
        return jsonify({"error": "Invalid email address"}), 400

    # Check for duplicates in users (case-insensitive)
    existing = User.query.filter(func.lower(User.email) == email).first()
    if existing:
        logger.warning("⚠️ Duplicate user registration attempt")
        return jsonify({"error": "User already exists"}), 400

    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    # Check for deleted (hashed) users (case-insensitive)
    email_hash = hash_string_secret(email)
    deleted = User.query.filter(func.lower(User.email) == email_hash).first()
    if deleted:
        logger.warning("⚠️ Deleted user registration attempt")

        # re-enable the deleted user
        deleted.email = email
        deleted.password = hashed
        deleted.first_name = first_name
        deleted.last_name = last_name
        deleted.signup_date = datetime.utcnow()
        db.session.commit()
        db.session.flush()

        logger.warning("⚠️ Deleted user registration successful")

        return jsonify({
            "message": "Welcome back to your AI Chef account"
        })

    # Create real user immediately
    user = User(
        email=email,
        password=hashed,
        first_name=first_name,
        last_name=last_name,
        signup_date=datetime.utcnow(),
    )

    db.session.add(user)
    db.session.flush()  # ensure user.id is populated

    # Create a confirmation token, but do not gate access on it
    try:
        raw_token = _generate_raw_token()
        ect = EmailConfirmToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        db.session.add(ect)
        db.session.commit()

        logger.info(f"✅ Registered new user: {email}")
        send_confirmation_email(email, raw_token)
    except Exception:
        # Do not block registration/log-in if email sending fails
        logger.exception("Failed to create/send confirmation token")
        db.session.commit()

    # Log the user in immediately so the app can start using the session
    try:
        login_user(user, remember=True, duration=timedelta(days=90))
    except Exception:
        logger.exception("Failed to log in user immediately after registration")

    return jsonify({
        "message": "Please check your email to confirm your AI Chef account"
    })


# Request Password Reset (no user enumeration)
@app.route("/api/request_password_reset", methods=["POST"])
def api_request_password_reset():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    # Always say 200
    user = User.query.filter(func.lower(User.email) == email).first()
    if user:
        # invalidate outstanding tokens
        PasswordResetToken.query.filter_by(user_id=user.id, used_at=None)\
            .update({PasswordResetToken.used_at: datetime.utcnow()})
        db.session.flush()

        raw = _generate_raw_token()
        prt = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw),
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=RESET_TTL_MINUTES),
            request_ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent", "")[:250],
        )
        db.session.add(prt)
        db.session.commit()

        try:
            base = app.config.get("RESET_LINK_BASE", "https://chef.zentha.me/reset")
            link = f"{base}?token={raw}"
            msg = Message(
                subject="Reset your AI Chef password",
                recipients=[user.email],
                body=(
                    "We received a request to reset your password.\n\n"
                    f"Open this link to set a new password (expires in {RESET_TTL_MINUTES} minutes):\n{link}\n\n"
                    "If you didn’t request this, ignore this email."
                ),
            )
            mail.send(msg)
        except Exception:
            logger.exception("Failed to send reset email")

    return jsonify({"message": "If that email exists, a reset link was sent."}), 200


# Confirmation (new)
@app.route("/confirm", methods=["GET"])
def confirm_page():
    """Finalize account via token and show a simple message."""
    raw = request.args.get("token", "", type=str)
    if not raw:
        return render_template_string(CONFIRM_PAGE_TEMPLATE, status="invalid"), 400

    # Primary path: new-style email confirmation tokens
    h = _hash_token(raw)
    ect = EmailConfirmToken.query.filter_by(token_hash=h).first()
    if ect:
        if ect.expires_at < datetime.utcnow():
            return render_template_string(CONFIRM_PAGE_TEMPLATE, status="expired"), 410

        user = ect.user or User.query.get(ect.user_id)
        if not user:
            return render_template_string(CONFIRM_PAGE_TEMPLATE, status="invalid"), 400

        if ect.used_at is not None or user.email_confirmed:
            return render_template_string(CONFIRM_PAGE_TEMPLATE, status="exists"), 200

        user.email_confirmed = True
        ect.used_at = datetime.utcnow()
        db.session.commit()
        return render_template_string(CONFIRM_PAGE_TEMPLATE, status="ok"), 200


@app.route("/reset", methods=["GET", "POST"])
def reset_page():
    """
    Temporary web UI for password reset.
    GET: show form if token valid, else show 'expired/invalid'.
    POST: set new password and show 'success—open the app'.
    """
    if request.method == "GET":
        raw = request.args.get("token", "", type=str)
        if not raw:
            return render_template_string(RESET_PAGE_TEMPLATE, invalid=True), 400
        h = _hash_token(raw)
        prt = PasswordResetToken.query.filter_by(token_hash=h).first()
        if not prt or prt.used_at is not None or prt.expires_at < datetime.utcnow():
            return render_template_string(RESET_PAGE_TEMPLATE, invalid=True), 400
        return render_template_string(RESET_PAGE_TEMPLATE, token=raw, invalid=False, done=False)

    # POST
    raw = request.form.get("token", "")
    pw1 = request.form.get("pw1", "")
    pw2 = request.form.get("pw2", "")
    if not raw or not pw1 or not pw2:
        return render_template_string(RESET_PAGE_TEMPLATE, invalid=True), 400
    if pw1 != pw2:
        return render_template_string(RESET_PAGE_TEMPLATE, token=raw, error="Passwords do not match"), 400
    if len(pw1) < 8:
        return render_template_string(RESET_PAGE_TEMPLATE, token=raw, error="Use at least 8 characters"), 400

    h = _hash_token(raw)
    prt = PasswordResetToken.query.filter_by(token_hash=h).first()
    if not prt or prt.used_at is not None or prt.expires_at < datetime.utcnow():
        return render_template_string(RESET_PAGE_TEMPLATE, invalid=True), 400

    user = prt.user
    user.password = bcrypt.hashpw(pw1.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    prt.used_at = datetime.utcnow()
    db.session.commit()

    # (Optional) invalidate other sessions here.

    return render_template_string(RESET_PAGE_TEMPLATE, done=True)


# Password Reset with token
@app.route("/api/reset_password", methods=["POST"])
def api_reset_password():
    data = request.get_json(silent=True) or {}
    raw = data.get("token") or ""
    new_pw = data.get("new_password") or ""
    if not raw or not new_pw:
        return jsonify({"error": "token and new_password required"}), 400
    if not _password_policy_ok(new_pw):
        return jsonify({"error": "Password does not meet policy"}), 400

    h = _hash_token(raw)
    prt = PasswordResetToken.query.filter_by(token_hash=h).first()
    if not prt or prt.used_at is not None or prt.expires_at < datetime.utcnow():
        return jsonify({"error": "Invalid or expired token"}), 400

    user = prt.user
    # set bcrypt hash
    user.password = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    prt.used_at = datetime.utcnow()

    # optional: invalidate other sessions here

    db.session.commit()
    return jsonify({"message": "Password updated"}), 200


# Change Password (logged in)
@app.route("/api/change_password", methods=["POST"])
@login_required
def api_change_password():
    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""
    if not new_pw:
        return jsonify({"error": "new_password required"}), 400
    if not _password_policy_ok(new_pw):
        return jsonify({"error": "Password does not meet policy"}), 400

    user = current_user  # User

    # If the account was created via Google and has no local password yet
    if not user.password or user.password == "":
        # allow setting initial local password without current_pw
        user.password = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        db.session.commit()
        return jsonify({"message": "Password set"}), 200

    # Normal flow: verify current
    ok = False
    try:
        ok = bcrypt.checkpw(current_pw.encode('utf-8'), user.password.encode('utf-8'))
    except Exception:
        ok = False
    if not ok:
        return jsonify({"error": "Current password incorrect"}), 401

    # Disallow reusing same password
    if bcrypt.checkpw(new_pw.encode('utf-8'), user.password.encode('utf-8')):
        return jsonify({"error": "New password must be different"}), 400

    user.password = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    # optional: invalidate other sessions here

    db.session.commit()
    return jsonify({"message": "Password changed"}), 200



@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "invalid_request", "message": "Email and password are required."}), 400

    user = User.query.filter_by(email=email).first()

    # Do not reveal whether the email exists (prevents account enumeration)
    if not user:
        return jsonify({"error": "invalid_credentials", "message": "Invalid credentials"}), 401

    # Handle social-only accounts (no password set)
    stored = (user.password or "").strip()
    if not stored:
        return jsonify({
            "error": "password_not_set",
            "message": "This account does not have a password. Sign in with Google/Apple or set a password."
        }), 401

    # Handle common bad storage: "b'$2b$...'" stored as text
    if stored.startswith("b'") or stored.startswith('b"'):
        try:
            import ast
            stored = ast.literal_eval(stored).decode("utf-8")
        except Exception:
            return jsonify({"error": "invalid_credentials", "message": "Invalid credentials"}), 401

    # Verify bcrypt safely
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except ValueError:
        ok = False

    if not ok:
        return jsonify({"error": "invalid_credentials", "message": "Invalid credentials"}), 401

    login_user(user, remember=True)

    return jsonify({
        "message": "Logged in",
        "user": {"id": user.id, "email": user.email}
    }), 200



# @app.route("/api/login", methods=["POST"])
# def login():
#     data = request.get_json() or {}
#     email = data.get("email", "").strip().lower()
#     password = data.get("password") or ""
#     user = User.query.filter_by(email=email).first()
#     if not user or not bcrypt.checkpw(password.encode('utf-8'), user.password.encode('utf-8')):
#         return jsonify({"error": "Invalid credentials"}), 401
#     login_user(user, remember=True)
#     # return jsonify({"message": "Logged in"})
#     return jsonify({
#         "message": "Logged in",
#         "user": {
#             "id": user.id,
#             "email": user.email
#         }
#     })


@app.route("/api/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Logged out"})


# Delete user account
@app.route("/api/delete_account", methods=["POST"])
@login_required
def delete_account():
    user = current_user
    user_id = user.id
    user_email = user.email
    user_name = user.first_name
    user_last_name = user.last_name

    try:
        recipes = Recipe.query.filter_by(user_id=user_id).all()
        for d in recipes:
            db.session.delete(d)

        # 2) Delete user-owned data
        EmailConfirmToken.query.filter_by(user_id=user.id).delete()
        UserSubscription.query.filter_by(user_id=user.id).delete()
        PasswordResetToken.query.filter_by(user_id=user.id).delete()
        user.email = hash_string_secret(user_email)
        user.first_name = f"Deleted-{user_name}"
        user.last_name = f"Deleted-{user_last_name}"
        user.language = "en"
        user.email_confirmed = False

        # Sever any social login links so they can't be used to log in again
        user.apple_user_id = None

        # Make password unusable
        user.password = ""

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        app.logger.exception("Failed to delete account for user_id=%s: %s", user_id, e)
        return jsonify({"error": "Failed to delete account"}), 500

    # 3) Log out the now-anonymized user
    logout_user()
    return jsonify({"success": True}), 200


def call_openai_with_retry(prompt, retries=3, delay=2):
    for attempt in range(retries):
        try:
            response = openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}]
            )
            return response
        except Exception as e:
            logger.warning(f"[GPT Retry] Attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


def _build_user_payload(chef_prompt: str, user_id: int, message: str, default_servings=None):
    try:
        u = User.query.get(user_id)   # your User model
    except Exception:
        logger.warning("user fetch/intro build failed", exc_info=True)
        intro = None

    parts = [chef_prompt]
    if default_servings:
        parts.append(
            f"Unless the request below explicitly states a different number of "
            f"servings or people, make this recipe for {default_servings} servings."
        )
    parts.append("Request:\n" + message.strip())
    return "\n\n".join(parts)


_INGREDIENT_UNITS = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
    "oz", "ounce", "ounces", "lb", "lbs", "pound", "pounds",
    "g", "gram", "grams", "kg", "kilogram", "kilograms",
    "ml", "milliliter", "milliliters", "l", "liter", "liters",
    "pint", "pints", "quart", "quarts", "gallon", "gallons",
    "clove", "cloves", "pinch", "pinches", "dash", "can", "cans",
    "slice", "slices", "stick", "sticks", "bunch", "bunches",
    "head", "heads", "piece", "pieces", "sprig", "sprigs",
}

_INGREDIENT_QTY_RE = re.compile(r"^\s*(?P<qty>\d+\s+\d+/\d+|\d+/\d+|\d+\.\d+|\d+)\s*")


def parse_ingredient_line(line):
    """
    Split one ingredient line ("2 cups flour", "1 tsp salt", "salt to taste")
    into {"quantity", "unit", "name"}. Falls back to quantity/unit=None and
    the full line as name whenever it can't confidently parse — never raises,
    that row just isn't scalable and stays editable as plain text.
    """
    text = line.strip().lstrip("-*•").strip()
    text = re.sub(r"^\d+[.)]\s*", "", text)  # strip leading "1." / "1)" list markers
    if not text:
        return None

    quantity = None
    unit = None
    name = text

    m = _INGREDIENT_QTY_RE.match(text)
    if m:
        quantity = m.group("qty")
        rest = text[m.end():].strip()
        if rest:
            first_word, _, remainder = rest.partition(" ")
            bare = first_word.strip(".,").lower()
            if bare in _INGREDIENT_UNITS:
                unit = first_word.strip(".,")
                name = remainder.strip() or first_word
            else:
                name = rest

    return {"quantity": quantity, "unit": unit, "name": name}


def parse_ingredients_block(block):
    if not block:
        return []
    items = []
    for line in block.splitlines():
        parsed = parse_ingredient_line(line)
        if parsed:
            items.append(parsed)
    return items


def parse_recipe_ai_response(content: str) -> dict:
    """
    Parse the AI response text into fields matching the Recipe model.
    Expects sections like:

    **Title:** ...
    **Description:** ...
    **Categories:** ...
    **Tags:** ...
    **Time:** ...
    **Servings:** ...
    **Ingredients:**
    - item
    - item

    **Instructions:**
    1. step
    2. step

    **Notes:** ...
    **Variations:** ...
    **Difficulty:** ...
    """

    # Detect markers (bold or plain)
    TITLE_MARK        = "**Title:**"        if "**Title:**"        in content else ("Title:"        if "Title:"        in content else None)
    DESCRIPTION_MARK  = "**Description:**"  if "**Description:**"  in content else ("Description:"  if "Description:"  in content else None)
    CATEGORIES_MARK   = "**Categories:**"   if "**Categories:**"   in content else ("Categories:"   if "Categories:"   in content else None)
    COURSE_MARK       = "**Course:**"       if "**Course:**"       in content else ("Course:"       if "Course:"       in content else None)
    MAINING_MARK      = "**Main Ingredient:**" if "**Main Ingredient:**" in content else ("Main Ingredient:" if "Main Ingredient:" in content else None)
    TAGS_MARK         = "**Tags:**"         if "**Tags:**"         in content else ("Tags:"         if "Tags:"         in content else None)
    TIME_MARK         = "**Time:**"         if "**Time:**"         in content else ("Time:"         if "Time:"         in content else None)
    SERVINGS_MARK     = "**Servings:**"     if "**Servings:**"     in content else ("Servings:"     if "Servings:"     in content else None)
    INGREDIENTS_MARK  = "**Ingredients:**"  if "**Ingredients:**"  in content else ("Ingredients:"  if "Ingredients:"  in content else None)
    INSTRUCTIONS_MARK = "**Instructions:**" if "**Instructions:**" in content else ("Instructions:" if "Instructions:" in content else None)
    # NOTES_MARK        = "**Notes:**"        if "**Notes:**"        in content else ("Notes:"        if "Notes:"        in content else None)
    VARIATIONS_MARK   = "**Variations:**"   if "**Variations:**"   in content else ("Variations:"   if "Variations:"   in content else None)
    DIFFICULTY_MARK   = "**Difficulty:**"   if "**Difficulty:**"   in content else ("Difficulty:"   if "Difficulty:"   in content else None)

    def _idx(mark):
        return content.find(mark) if mark else -1

    # Positions
    iTitle  = _idx(TITLE_MARK)
    iDesc   = _idx(DESCRIPTION_MARK)
    iCats   = _idx(CATEGORIES_MARK)
    iCourse = _idx(COURSE_MARK)
    iMainI  = _idx(MAINING_MARK)
    iTags   = _idx(TAGS_MARK)
    iTime   = _idx(TIME_MARK)
    iServ   = _idx(SERVINGS_MARK)
    iIngr   = _idx(INGREDIENTS_MARK)
    iInstr  = _idx(INSTRUCTIONS_MARK)
    # iNotes  = _idx(NOTES_MARK)
    iVars   = _idx(VARIATIONS_MARK)
    iDiff   = _idx(DIFFICULTY_MARK)

    all_indices = [
        i for i in [
            iTitle, iDesc, iCats, iCourse, iMainI, iTags, iTime, iServ,
            iIngr, iInstr, iVars, iDiff
            # iIngr, iInstr, iNotes, iVars, iDiff
        ] if i != -1
    ]

    def _next_idx(current_idx: int) -> int:
        if current_idx == -1:
            return -1
        higher = [i for i in all_indices if i > current_idx]
        return min(higher) if higher else -1

    def slice_between(start_mark, start_idx, end_idx):
        if start_idx == -1 or not start_mark:
            return None
        start = start_idx + len(start_mark)
        end   = len(content) if end_idx == -1 else end_idx
        return content[start:end].strip()

    # Core fields
    title = slice_between(TITLE_MARK, iTitle, _next_idx(iTitle))
    description = slice_between(DESCRIPTION_MARK, iDesc, _next_idx(iDesc))

    # Categories
    categories_block = slice_between(CATEGORIES_MARK, iCats, _next_idx(iCats))
    if categories_block:
        categories_list = [
            c.strip()
            for line in categories_block.splitlines()
            for c in line.split(",")
            if c.strip()
        ]
    else:
        categories_list = []
    categories_str = ", ".join(categories_list) if categories_list else None

    # Course / Main Ingredient — map free AI text onto the fixed taxonomy.
    # Case-insensitive exact match only; anything that doesn't match a known
    # value is dropped (left None) rather than polluting the taxonomy with
    # AI-invented strings.
    def _match_taxonomy(raw_block, choices, max_matches):
        if not raw_block:
            return None
        candidates = [
            c.strip()
            for line in raw_block.splitlines()
            for c in line.split(",")
            if c.strip()
        ]
        lookup = {choice.lower(): choice for choice in choices}
        matched = []
        for cand in candidates:
            hit = lookup.get(cand.strip().lower())
            if hit and hit not in matched:
                matched.append(hit)
            if len(matched) >= max_matches:
                break
        return ", ".join(matched) if matched else None

    course_block = slice_between(COURSE_MARK, iCourse, _next_idx(iCourse))
    course_str = _match_taxonomy(course_block, RECIPE_COURSES, max_matches=1)

    main_ingredient_block = slice_between(MAINING_MARK, iMainI, _next_idx(iMainI))
    main_ingredient_str = _match_taxonomy(main_ingredient_block, RECIPE_MAIN_INGREDIENTS, max_matches=2)

    # Tags
    tags_block = slice_between(TAGS_MARK, iTags, _next_idx(iTags))
    if tags_block:
        tags_list = [
            t.strip().lstrip("#")
            for line in tags_block.splitlines()
            for t in line.split(",")
            if t.strip()
        ]
    else:
        tags_list = []
    tags_str = ", ".join(tags_list) if tags_list else None

    time_text = slice_between(TIME_MARK, iTime, _next_idx(iTime))
    servings_text = slice_between(SERVINGS_MARK, iServ, _next_idx(iServ))

    ingredients_block = slice_between(INGREDIENTS_MARK, iIngr, _next_idx(iIngr))
    instructions_block = slice_between(INSTRUCTIONS_MARK, iInstr, _next_idx(iInstr))

    # notes = slice_between(NOTES_MARK, iNotes, _next_idx(iNotes))
    variations = slice_between(VARIATIONS_MARK, iVars, _next_idx(iVars))

    difficulty_block = slice_between(DIFFICULTY_MARK, iDiff, _next_idx(iDiff))
    difficulty = None
    if difficulty_block:
        import string as _string
        difficulty = difficulty_block.splitlines()[0].strip().strip(_string.punctuation)

    # Fallback: nothing parsed → treat whole block as instructions
    if not any([
        title, description, categories_list, tags_list,
        time_text, servings_text, ingredients_block, instructions_block
    ]):
        instructions_block = content.strip()

    return {
        "title":                title or None,
        "description":          description or None,
        "categories":           categories_str,
        "course":               course_str,
        "main_ingredient":      main_ingredient_str,
        "tags":                 tags_str,
        "time":                 time_text or None,
        "servings":             servings_text or None,
        "ingredients":          ingredients_block or None,
        "ingredients_structured": parse_ingredients_block(ingredients_block),
        "instructions":         instructions_block or None,
        # "notes":        notes or None,
        "variations":           variations or None,
        "difficulty":           difficulty or None,
    }


# --- Canonical recipe cache -------------------------------------------------
# Generic requests ("chicken parmesan") get matched against a shared,
# de-duplicated recipe cache by embedding similarity before falling back to a
# fresh OpenAI call. A miss generates normally (unchanged flow) and also gets
# inserted into the cache, so the next similar request is a hit.

_GENERIC_DENYLIST = [
    "keto", "gluten free", "gluten-free", "dairy free", "dairy-free", "vegan",
    "vegetarian", "paleo", "low carb", "low-carb", "sugar free", "sugar-free",
    "nut free", "nut-free", "allerg", "substitute", "instead of", "leftover",
    "i have", "using my", "on hand", "no oven", "air fryer", "instant pot",
    "slow cooker", "crock pot", "servings", "people", "spicy", "quick",
    "under ", "minutes", "healthier", "cheaper", "budget", "make this",
]


def _is_generic_request(message):
    """
    Only bare dish/category asks ("chicken parmesan", "banana bread") are
    eligible for the canonical-recipe cache. Anything with dietary, quantity,
    time-constraint, or substitution language skips it. When uncertain, this
    returns False — a false negative just means a normal fresh generation;
    a false positive would mean serving someone the wrong recipe, which is
    the failure mode to avoid.
    """
    text = (message or "").strip().lower()
    if not text or len(text.split()) > 8:
        return False
    if re.search(r"\d", text):
        return False
    if any(word in text for word in _GENERIC_DENYLIST):
        return False
    return True


def embed_text(text):
    resp = client.embeddings.create(model="text-embedding-3-small", input=text)
    return resp.data[0].embedding


def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


CANONICAL_MATCH_THRESHOLD = 0.88  # tune empirically once there's real traffic


def find_canonical_match(embedding):
    best, best_score = None, 0.0
    candidates = CanonicalRecipe.query.all()
    for cand in candidates:
        if not cand.embedding:
            continue
        try:
            cand_vec = json.loads(cand.embedding)
        except (TypeError, ValueError):
            continue
        score = cosine_similarity(embedding, cand_vec)
        if score > best_score:
            best, best_score = cand, score

    if best is None:
        logger.info(f"Canonical match: no candidates with an embedding (canonical_recipe rows: {len(candidates)})")
    else:
        logger.info(
            f"Canonical match: best={best.id} ({best.title!r}) score={best_score:.4f} "
            f"threshold={CANONICAL_MATCH_THRESHOLD} candidates={len(candidates)}"
        )

    return best if (best is not None and best_score >= CANONICAL_MATCH_THRESHOLD) else None


def build_recipe_image_prompt(title, description, course, main_ingredient):
    context = ". ".join(p for p in [title, description] if p) or "a home-cooked dish"
    tags = ", ".join(p for p in [course, main_ingredient] if p)
    return (
        f"A professional, appetizing food-photography style image of: {context}. "
        + (f"Category: {tags}. " if tags else "")
        + "Overhead or 3/4 angle shot, natural lighting, on a simple plate or in a "
          "pan, no text or watermarks."
    )


def generate_recipe_image_bytes(prompt):
    image_response = client.images.generate(
        model="gpt-image-1.5",
        prompt=prompt,
        n=1,
        size="1024x1024",
    )
    b64 = image_response.data[0].b64_json
    return base64.b64decode(b64)


# Absolute, hardcoded (env-overridable) target — deliberately NOT derived
# from __file__ or the process's working directory. Both were tried and
# both broke: this app.py is apparently deployed/run from inside
# /data/dreamr-frontend (shared with dreamr, per the venv path also being
# /opt/dreamr-venv), so anything relative to "where this script lives" or
# "where the process started" resolves into dreamr's tree instead of
# chef's. This is the one path that's actually unambiguous.
CHEF_STATIC_IMAGE_DIR = os.getenv("CHEF_STATIC_IMAGE_DIR", "/data/chef-frontend/static/images/recipes")


def save_recipe_image(img_bytes):
    filename = f"{uuid.uuid4().hex}.png"
    image_path = os.path.join(CHEF_STATIC_IMAGE_DIR, filename)
    os.makedirs(os.path.dirname(image_path), exist_ok=True)
    with open(image_path, "wb") as f:
        f.write(img_bytes)
    return filename


def _generate_image_background(recipe_id=None, canonical_id=None):
    """
    Runs on its own thread, decoupled from the request/response cycle
    entirely — once /api/chat has returned, this keeps running against the
    database regardless of whether the client is still connected. If both
    recipe_id and canonical_id are given, one image is generated and written
    to both rows (avoids paying for the same image twice). Needs its own app
    context since it runs outside the request that spawned it — this is the
    standard Flask-SQLAlchemy pattern for background work, giving this
    thread its own scoped session rather than sharing the request's.

    Caveat worth knowing: this is a daemon thread, not a durable job queue.
    If this specific worker process gets recycled mid-generation (a deploy,
    a restart, gunicorn rotating workers), the thread dies with it — no
    error, just no image. That's what the nightly backfill job is for.
    """
    with app.app_context():
        try:
            recipe = Recipe.query.get(recipe_id) if recipe_id else None
            canonical = CanonicalRecipe.query.get(canonical_id) if canonical_id else None
            if recipe is None and canonical is None:
                return

            source = canonical or recipe
            prompt = build_recipe_image_prompt(
                source.title, source.description, source.course, source.main_ingredient
            )
            img_bytes = generate_recipe_image_bytes(prompt)
            filename = save_recipe_image(img_bytes)

            if recipe is not None:
                recipe.image_file = filename
            if canonical is not None:
                canonical.image_file = filename
                canonical.image_prompt = prompt
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Background image generation failed (non-fatal)")


def _start_background_image_generation(recipe_id=None, canonical_id=None):
    threading.Thread(
        target=_generate_image_background,
        kwargs={"recipe_id": recipe_id, "canonical_id": canonical_id},
        daemon=True,
    ).start()


def _cache_canonical_recipe(message, parsed, content):
    """
    Best-effort: embed + insert a freshly generated recipe into the canonical
    cache. Deliberately does NOT generate an image inline — that used to run
    synchronously as part of this (and therefore of /api/chat's response
    time), adding real seconds/cost to every cache-miss request. The caller
    is expected to kick off _start_background_image_generation with the
    returned row's id instead. Returns the new CanonicalRecipe on success,
    None on failure — never raises; a cache write failure shouldn't break
    the user's actual recipe response.
    """
    try:
        embedding = embed_text(message)
        canonical = CanonicalRecipe(
            title=parsed.get("title"),
            description=parsed.get("description"),
            course=parsed.get("course"),
            main_ingredient=parsed.get("main_ingredient"),
            tags=parsed.get("tags"),
            time=parsed.get("time"),
            servings=parsed.get("servings"),
            ingredients_json=json.dumps(parsed.get("ingredients_structured") or []),
            instructions=parsed.get("instructions"),
            variations=parsed.get("variations"),
            difficulty=parsed.get("difficulty"),
            ai_response=content,
            embedding=json.dumps(embedding),
            hit_count=0,
        )
        db.session.add(canonical)
        db.session.commit()
        return canonical
    except Exception:
        db.session.rollback()
        logger.exception("Failed to cache canonical recipe (non-fatal)")
        return None


# generate recipe
@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    """
    Chef chat endpoint: takes a free-form user prompt (message),
    generates a structured recipe via OpenAI, stores it in Recipe,
    and returns the parsed fields as JSON.
    """
    logger.info(" /api/chat called")
    data = request.get_json() or {}
    logger.debug(f"Received JSON: {data}")

    message = (data.get("message") or "").strip()
    if not message:
        logger.debug("[WARN] Missing request message.")
        return jsonify({"error": "Missing request message."}), 400

    try:
        default_servings = int(data.get("default_servings")) if data.get("default_servings") else None
        if default_servings is not None and not (1 <= default_servings <= 50):
            default_servings = None
    except (TypeError, ValueError):
        default_servings = None

    # Check plan and possibly decrement text quota
    decremented_text = False
    is_pro = True
    #is_pro = _user_is_pro(current_user.id)

    try:
        if not is_pro:
            ok, reset_iso = decrement_text_or_deny(current_user.id)
            if not ok:
                return jsonify(
                    {
                        "error": "quota_exhausted",
                        "kind": "text",
                        "next_reset_iso": reset_iso,
                    }
                ), 402
            decremented_text = True

        # 1) Save the raw user request as a Recipe shell
        logger.info("Saving request to database...")
        recipe = Recipe(
            user_id=current_user.id,
            text=message,
            created_at=datetime.utcnow(),
        )
        db.session.add(recipe)
        db.session.flush()  # get recipe.id without full commit yet
        logger.debug(f"Recipe shell saved with ID: {recipe.id}")

        # 1b) Canonical-recipe cache: for a bare/generic dish request, try to
        # match an existing shared recipe by embedding similarity before
        # paying for a fresh OpenAI generation. See find_canonical_match().
        is_generic = _is_generic_request(message)
        if is_generic:
            try:
                request_embedding = embed_text(message)
                match = find_canonical_match(request_embedding)
            except Exception:
                logger.exception("Canonical match lookup failed (non-fatal, generating fresh)")
                match = None

            if match is not None:
                logger.info(f"Canonical cache hit: recipe {recipe.id} <- canonical {match.id}")
                recipe.ai_response       = match.ai_response
                recipe.title             = match.title
                recipe.description       = match.description
                recipe.course            = match.course
                recipe.main_ingredient   = match.main_ingredient
                recipe.categories        = ", ".join(v for v in [match.course, match.main_ingredient] if v) or None
                recipe.tags              = match.tags
                recipe.time              = match.time
                recipe.servings          = match.servings
                recipe.ingredients_json  = match.ingredients_json
                recipe.ingredients       = "\n".join(
                    f"- {' '.join(p for p in [i.get('quantity'), i.get('unit'), i.get('name')] if p)}"
                    for i in json.loads(match.ingredients_json or "[]")
                ) or None
                recipe.instructions      = match.instructions
                recipe.variations        = match.variations
                recipe.difficulty        = match.difficulty
                recipe.source_canonical_id = match.id
                # Free — the canonical entry already has an image from a
                # previous request. If it doesn't yet (still generating in
                # the background, or that worker got recycled before it
                # finished), kick off a background job below instead of
                # leaving this recipe permanently imageless.
                if match.image_file:
                    recipe.image_file = match.image_file

                match.hit_count = (match.hit_count or 0) + 1
                db.session.commit()
                logger.info(f"Recipe {recipe.id} served from canonical cache")

                if not match.image_file:
                    _start_background_image_generation(recipe_id=recipe.id, canonical_id=match.id)

                return jsonify({
                    "recipe_id":    recipe.id,
                    "title":        recipe.title,
                    "description":  recipe.description,
                    "categories":   recipe.categories,
                    "course":       recipe.course,
                    "main_ingredient": recipe.main_ingredient,
                    "tags":         recipe.tags,
                    "time":         recipe.time,
                    "servings":     recipe.servings,
                    "ingredients":  recipe.ingredients,
                    "ingredients_json": recipe.ingredients_json,
                    "instructions": recipe.instructions,
                    "notes":        recipe.notes,
                    "variations":   recipe.variations,
                    "difficulty":   recipe.difficulty,
                }), 200

        # 2) Build prompt
        # For now, use the same chef prompt for both free and pro.
        # If you add a 'chef_free' key to CATEGORY_PROMPTS, switch to that here.
        chef_prompt = CATEGORY_PROMPTS.get("chef")
        if not chef_prompt:
            logger.error("CATEGORY_PROMPTS['chef'] is missing")
            raise RuntimeError("Chef prompt not configured")

        prompt = _build_user_payload(chef_prompt, current_user.id, message, default_servings=default_servings)
        logger.info(f"Sending prompt to OpenAI (pro={is_pro})")
        logger.debug(f"Chef prompt: {prompt}")

        # 3) Call OpenAI
        response = call_openai_with_retry(prompt)
        if not getattr(response, "choices", None) or not response.choices[0].message:
            logger.error("[ERROR] AI response was empty.")
            raise RuntimeError("AI response was empty")

        content = response.choices[0].message.content.strip()
        logger.debug(f"Chef reply: {content!r}")

        # 4) Parse content into recipe fields
        parsed = parse_recipe_ai_response(content)

        # 5) Update Recipe with parsed fields and raw AI response
        recipe.ai_response  = content
        recipe.title        = parsed.get("title")
        recipe.description  = parsed.get("description")
        recipe.course           = parsed.get("course")
        recipe.main_ingredient  = parsed.get("main_ingredient")
        # Legacy free-text categories column, kept as a display fallback for
        # code paths that haven't been migrated to course/main_ingredient yet.
        recipe.categories   = ", ".join(
            v for v in [recipe.course, recipe.main_ingredient] if v
        ) or None
        recipe.tags         = parsed.get("tags")
        recipe.time         = parsed.get("time")
        recipe.servings     = parsed.get("servings")
        recipe.ingredients  = parsed.get("ingredients")
        recipe.ingredients_json = json.dumps(parsed.get("ingredients_structured") or [])
        recipe.instructions = parsed.get("instructions")
        recipe.notes        = parsed.get("notes")
        recipe.variations   = parsed.get("variations")
        recipe.difficulty   = parsed.get("difficulty")

        db.session.commit()
        logger.info(f"Recipe {recipe.id} committed successfully")

        # 5b) Kick off this recipe's image in the background — the response
        # below doesn't wait on it. If this was a generic request, also
        # cache it into canonical_recipe (best-effort) so the next similar
        # request is a hit, and generate one shared image for both rows
        # instead of paying for it twice.
        if is_generic:
            canonical = _cache_canonical_recipe(message, parsed, content)
            _start_background_image_generation(
                recipe_id=recipe.id,
                canonical_id=canonical.id if canonical else None,
            )
        else:
            _start_background_image_generation(recipe_id=recipe.id)

        # 6) Return parsed recipe to frontend
        return jsonify(
            {
                "recipe_id":    recipe.id,
                "title":        recipe.title,
                "description":  recipe.description,
                "categories":   recipe.categories,
                "course":       recipe.course,
                "main_ingredient": recipe.main_ingredient,
                "tags":         recipe.tags,
                "time":         recipe.time,
                "servings":     recipe.servings,
                "ingredients":  recipe.ingredients,
                "ingredients_json": recipe.ingredients_json,
                "instructions": recipe.instructions,
                "notes":        recipe.notes,
                "variations":   recipe.variations,
                "difficulty":   recipe.difficulty,
            }
        ), 200

    except Exception:
        db.session.rollback()
        if decremented_text and not is_pro:
            refund_text(current_user.id)
        logger.error("Exception during Chef chat", exc_info=True)
        return jsonify({"error": "internal error"}), 500



# all recipes need to be displayed in the manage page
@app.route("/api/allrecipes", methods=["GET"])
@login_required
def get_allrecipes():
    user_tz = ZoneInfo(current_user.timezone or "UTC")

    recipes = (
        Recipe.query
        .filter(Recipe.user_id == current_user.id)
        .order_by(Recipe.created_at.desc())
        .all()
    )

    def convert_created_at(dt):
        try:
            print(f"Original datetime: {dt} (tzinfo={dt.tzinfo})")
            return dt.replace(tzinfo=timezone.utc).astimezone(user_tz).isoformat()
        except Exception as e:
            print(f"[ERROR] Timestamp conversion failed: {e}")
            traceback.print_exc()
            return None

    return jsonify([
        {
            "id": r.id,
            "title": r.title,
            "description": r.description,
            "categories": r.categories,
            "course": r.course,
            "main_ingredient": r.main_ingredient,
            "tags": r.tags,
            "time": r.time,
            "servings": r.servings,
            "ingredients": r.ingredients,
            "ingredients_json": r.ingredients_json,
            "instructions": r.instructions,
            "notes": r.notes,
            "variations": r.variations,
            "difficulty": r.difficulty,
            "text": r.text,                    # original user prompt
            "ai_response": r.ai_response,      # full raw AI output
            "image_file": f"/static/images/recipes/{r.image_file}" if r.image_file else None,
            "created_at": convert_created_at(r.created_at) if r.created_at else None,
        }
        for r in recipes
    ])



# fetch recipes
@app.route("/api/recipes", methods=["GET"])
@login_required
def get_recipes():
    user_tz = ZoneInfo(current_user.timezone or "UTC")

    recipes = Recipe.query.filter(
        Recipe.user_id == current_user.id,
        or_(Recipe.archived == False, Recipe.archived.is_(None))
    ).order_by(Recipe.created_at.desc()).all()

    def convert_created_at(dt):
        try:
            print(f"Original datetime: {dt} (tzinfo={dt.tzinfo})")
            return dt.replace(tzinfo=timezone.utc).astimezone(user_tz).isoformat()
        except Exception as e:
            print(f"[ERROR] Timestamp conversion failed: {e}")
            traceback.print_exc()
            return None

    return jsonify([
        {
            "id": r.id,
            "title": r.title,
            "description": r.description,
            "difficulty": r.difficulty,
            "categories": r.categories,
            "course": r.course,
            "main_ingredient": r.main_ingredient,
            "ingredients": r.ingredients,
            "ingredients_json": r.ingredients_json,
            "instructions": r.instructions,
            "variations": r.variations,
            "tags": r.tags,
            "time": r.time,
            "servings": r.servings,
            "text": r.text,               # original user prompt
            "ai_response": r.ai_response, # full AI reply
            "notes": r.notes,
            "image_file": f"/static/images/recipes/{r.image_file}" if r.image_file else None,
            "created_at": convert_created_at(r.created_at) if r.created_at else None,
        }
        for r in recipes
    ])


# For deleting recipes
@app.route("/api/recipes/<int:recipe_id>", methods=["DELETE"])
@login_required
def delete_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    if recipe.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # # Move image file to archive folder (if you ever use images for recipes)
    # if recipe.image_file:
    #     try:
    #         image_path = os.path.join("static", "images", "recipes", recipe.image_file)
    #         archive_dir = os.path.join("static", "images", "recipes", "deleted")

    #         os.makedirs(archive_dir, exist_ok=True)

    #         if os.path.exists(image_path):
    #             shutil.move(
    #                 image_path,
    #                 os.path.join(archive_dir, os.path.basename(image_path))
    #             )

    #     except Exception as e:
    #         print(f"[WARN] Failed to archive recipe image: {e}")

    db.session.delete(recipe)
    db.session.commit()
    return "", 204


# For editing recipes (title, course/main ingredient, ingredients, instructions, etc.)
@app.route("/api/recipes/<int:recipe_id>", methods=["PATCH"])
@login_required
def update_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    if recipe.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json() or {}

    if "title" in data:
        recipe.title = (data.get("title") or "").strip() or None
    if "description" in data:
        recipe.description = (data.get("description") or "").strip() or None

    if "course" in data:
        course = (data.get("course") or "").strip()
        if course and course not in RECIPE_COURSES:
            return jsonify({"error": f"Invalid course: {course}"}), 400
        recipe.course = course or None

    if "main_ingredient" in data:
        raw = data.get("main_ingredient")
        values = raw if isinstance(raw, list) else str(raw or "").split(",")
        values = [v.strip() for v in values if v and v.strip()]
        for v in values:
            if v not in RECIPE_MAIN_INGREDIENTS:
                return jsonify({"error": f"Invalid main_ingredient: {v}"}), 400
        recipe.main_ingredient = ", ".join(values) or None

    if "tags" in data:
        recipe.tags = (data.get("tags") or "").strip() or None
    if "time" in data:
        recipe.time = (data.get("time") or "").strip() or None
    if "servings" in data:
        recipe.servings = (data.get("servings") or "").strip() or None
    if "instructions" in data:
        recipe.instructions = (data.get("instructions") or "").strip() or None
    if "notes" in data:
        recipe.notes = (data.get("notes") or "").strip() or None
    if "variations" in data:
        recipe.variations = (data.get("variations") or "").strip() or None
    if "difficulty" in data:
        recipe.difficulty = (data.get("difficulty") or "").strip() or None

    if "ingredients" in data:
        raw_items = data.get("ingredients") or []
        if not isinstance(raw_items, list):
            return jsonify({"error": "ingredients must be a list"}), 400
        clean_items = []
        text_lines = []
        for item in raw_items:
            if not isinstance(item, dict):
                return jsonify({"error": "each ingredient must be an object"}), 400
            name = (item.get("name") or "").strip()
            if not name:
                continue
            quantity = (item.get("quantity") or "").strip() or None
            unit = (item.get("unit") or "").strip() or None
            clean_items.append({"quantity": quantity, "unit": unit, "name": name})
            text_lines.append(" ".join(p for p in [quantity, unit, name] if p))
        recipe.ingredients_json = json.dumps(clean_items)
        # Regenerate the legacy free-text column too, so anything still
        # reading recipe.ingredients (sharing, PDF export, etc.) stays correct.
        recipe.ingredients = "\n".join(f"- {line}" for line in text_lines) or None

    # Keep the legacy free-text categories column roughly in sync for any
    # display path that hasn't been migrated to course/main_ingredient yet.
    if "course" in data or "main_ingredient" in data:
        recipe.categories = ", ".join(
            v for v in [recipe.course, recipe.main_ingredient] if v
        ) or None

    db.session.commit()

    return jsonify({
        "id":               recipe.id,
        "title":            recipe.title,
        "description":      recipe.description,
        "categories":       recipe.categories,
        "course":           recipe.course,
        "main_ingredient":  recipe.main_ingredient,
        "tags":             recipe.tags,
        "time":             recipe.time,
        "servings":         recipe.servings,
        "ingredients":      recipe.ingredients,
        "ingredients_json": recipe.ingredients_json,
        "instructions":     recipe.instructions,
        "notes":            recipe.notes,
        "variations":       recipe.variations,
        "difficulty":       recipe.difficulty,
        "image_file": f"/static/images/recipes/{recipe.image_file}" if recipe.image_file else None,
    })


@app.route("/api/recipes/<int:recipe_id>/toggle-archived", methods=["POST"])
@login_required
def toggle_archived_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    if recipe.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # archived is a Boolean; treat None as False
    recipe.archived = not bool(recipe.archived)
    db.session.commit()

    return jsonify({"archived": recipe.archived})


# Generate (or regenerate) an AI image for one of the user's own recipes.
# Ported from dreamr's /api/image_generate (dreamr/back_end/dreamr/app.py) —
# chef's Flutter client (ApiService.generateRecipeImage) already calls this
# route and expects {"image": "<path>"}; this was previously missing entirely.
@app.route("/api/image_generate", methods=["POST"])
@login_required
def image_generate():
    # Quota enforcement is disabled app-wide for now (see is_pro=True in
    # chat()) — mirrored here for consistency rather than being the one
    # endpoint that actually enforces it.
    is_pro = True
    # is_pro = _user_is_pro(current_user.id)

    data = request.get_json() or {}
    recipe_id = data.get("recipe_id")
    if not recipe_id:
        return jsonify({"error": "Missing recipe_id."}), 400

    recipe = Recipe.query.get(recipe_id)
    if recipe is None or recipe.user_id != current_user.id:
        return jsonify({"error": "Recipe not found or unauthorized"}), 404

    decremented_image = False
    try:
        if not is_pro:
            ok = decrement_image_or_deny(current_user.id)
            if not ok:
                return jsonify({"error": "quota_exhausted", "kind": "image"}), 402
            decremented_image = True

        prompt = build_recipe_image_prompt(recipe.title, recipe.description, recipe.course, recipe.main_ingredient)
        img_bytes = generate_recipe_image_bytes(prompt)
        recipe.image_file = save_recipe_image(img_bytes)
        db.session.commit()

        return jsonify({"image": f"/static/images/recipes/{recipe.image_file}"})

    except openai.OpenAIError:
        db.session.rollback()
        if decremented_image:
            refund_image(current_user.id)
        logger.error("OpenAI image generation failed", exc_info=True)
        return jsonify({"error": "OpenAI image generation failed"}), 502
    except Exception:
        db.session.rollback()
        if decremented_image:
            refund_image(current_user.id)
        logger.exception("Unexpected error during recipe image generation")
        return jsonify({"error": "Image generation failed"}), 500


@app.route("/api/check_auth", methods=["GET"])
def check_auth():
    if current_user.is_authenticated:
        return jsonify({
            "authenticated": True,
            "first_name": current_user.first_name,
            # "enable_audio": current_user.enable_audio,
            # "email": current_user.email
        })
    return jsonify({"authenticated": False}), 401


# --- Subscription API Endpoints ---
@app.route("/api/subscription/status", methods=["GET"])
@login_required
def get_subscription_status():
    """Get the current subscription status for the logged-in user"""
    try:
        status = SubscriptionService.get_user_subscription_status(current_user.id)

        # If not subscribed, attach counters
        tier = (status.get("tier") or "").lower()
        if status.get("is_active") and (tier.startswith("pro") or tier.startswith("trial")):
            return jsonify(status)

        uc = ensure_week_current(current_user.id)
        status.update({
            "text_remaining_week": uc.text_remaining_week,
            "image_remaining_lifetime": uc.image_remaining_lifetime,
            "next_reset_iso": next_reset_iso(current_user.id),
        })
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error fetching subscription status: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch subscription status"}), 500

@app.route("/api/subscription/plans", methods=["GET"])
@login_required
def get_subscription_plans():
    """Get all available subscription plans"""
    try:
        plans = SubscriptionService.get_subscription_plans()
        return jsonify(plans)
    except Exception as e:
        logger.error(f"Error fetching subscription plans: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch subscription plans"}), 500

@app.route("/api/subscription/purchase", methods=["POST"])
@login_required
def purchase_subscription():
    """Initiate a subscription purchase
    """
    data = request.get_json(silent=True) or {}
    raw_plan = data.get("plan_id")

    if not raw_plan:
        return jsonify({"error": "plan_id is required"}), 400

    # Allow lookup by primary key OR by product_id used in the stores
    plan = SubscriptionPlan.query.get(raw_plan)
    if not plan:
        plan = SubscriptionPlan.query.filter_by(product_id=raw_plan).first()
    if not plan:
        return jsonify({"error": f"Plan {raw_plan} not found"}), 404

    try:
        # Determine payment provider
        payment_provider = data.get("payment_provider")
        receipt_data = data.get("receipt_data")

        # Initiate subscription (pass the canonical plan id)
        result = SubscriptionService.initiate_subscription(
            user_id=current_user.id,
            plan_id=plan.id,
            payment_provider=payment_provider,
            receipt_data=receipt_data,
        )

        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error initiating subscription: {e}", exc_info=True)
        return jsonify({"error": "Failed to initiate subscription"}), 500

@app.route("/api/subscription/cancel", methods=["POST"])
@login_required
def cancel_subscription():
    """Cancel the current subscription"""
    try:
        success = SubscriptionService.cancel_subscription(current_user.id)
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Error canceling subscription: {e}", exc_info=True)
        return jsonify({"error": "Failed to cancel subscription"}), 500

@app.route("/api/subscription/payment-method", methods=["POST"])
@login_required
def update_payment_method():
    """Update the payment method for the current subscription"""
    data = request.get_json(silent=True) or {}

    try:
        success = SubscriptionService.update_payment_method(current_user.id, data)
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Error updating payment method: {e}", exc_info=True)
        return jsonify({"error": "Failed to update payment method"}), 500

# added by AZAD, not used
@app.route("/api/admin/subscription/force_set", methods=["POST"])
@admin_required
def admin_force_set_subscription():
    """Admin-only endpoint to manually create or repair a user's subscription.

    Useful when the store reports an active subscription but the local DB is
    out of sync and requires repairing a single user record.
    """
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    plan_id = data.get("plan_id")
    months = data.get("months")
    years = data.get("years")

    if not user_id or not plan_id:
        return jsonify({"error": "user_id and plan_id are required"}), 400

    try:
        sub = SubscriptionService.upsert_manual_subscription(
            user_id=int(user_id),
            plan_id=str(plan_id),
            months=months,
            years=years,
            payment_provider="admin-force",
        )
        return jsonify({
            "success": True,
            "subscription": {
                "id": sub.id,
                "user_id": sub.user_id,
                "plan_id": sub.plan_id,
                "status": sub.status,
                "start_date": sub.start_date.isoformat(),
                "end_date": sub.end_date.isoformat() if sub.end_date else None,
            },
        })
    except Exception as e:
        logger.error("admin_force_set_subscription error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500

# --- Optional: Webhook Handlers for App Store and Google Play ---
@app.route("/api/webhooks/apple-iap", methods=["POST"])
def apple_iap_webhook():
    """
    Handle Apple App Store Server Notifications

    This endpoint receives server-to-server notifications from Apple
    about subscription events (renewals, cancellations, etc.)
    """
    data = request.get_json(silent=True) or {}
    logger.info(f"Received Apple IAP webhook: {data}")

    # Process the notification (implementation depends on your business logic)
    # ...

    return jsonify({"status": "received"}), 200

@app.route("/api/webhooks/google-play", methods=["POST"])
def google_play_webhook():
    """
    Handle Google Play Developer API Notifications

    This endpoint receives server-to-server notifications from Google
    about subscription events (renewals, cancellations, etc.)
    """
    data = request.get_json(silent=True) or {}
    logger.info(f"Received Google Play webhook: {data}")

    # Process the notification (implementation depends on your business logic)
    # ...

    return jsonify({"status": "received"}), 200


# =========================
# Admin blueprint (HTML)
# =========================
# admin_bp = Blueprint("admin", __name__, url_prefix="/admin") breaks app

# Minimal inline templates to avoid files
ADMIN_SHELL = """<!doctype html><meta charset="utf-8">
<title>{{ title or 'Admin' }}</title>
<style>
  /* page width */
  :root { --page-width: 1400px; }               /* make larger if you want */
  html,body{height:100%;margin:0;padding:0}
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;color:#151515;background:#fff}
  .wrap{width:min(96vw, var(--page-width)); margin:20px auto; padding:0 8px;}

  a{color:#0a58ca;text-decoration:none}
  .msg{padding:6px 10px;border-radius:8px;background:#eef;display:inline-block}
  nav a{margin-right:12px}
  hr{border:0;border-top:1px solid #ddd;margin:12px 0}

  table{border-collapse:collapse;width:100%;margin:10px 0; table-layout:auto}
  th,td{border:1px solid #ddd;padding:8px;vertical-align:top}
  th{background:#f4f4f4}
</style>

<div class="wrap">
  <h1>AI Chef Admin</h1>
  <nav>
    <a href="/admin/">Dashboard</a>
    <a href="/admin/users">Users</a>
    <a href="/admin/logout" onclick="event.preventDefault();document.getElementById('al').submit()">Logout</a>
  </nav>
  <hr>
  {{ body|safe }}
  <form id="al" method="post" action="/admin/logout"></form>
</div>
"""


def _render_admin(body_tpl: str, title: str, **ctx):
    body = render_template_string(body_tpl, **ctx)
    return render_template_string(ADMIN_SHELL, title=title, body=body)

# --- Admin login form (HTML) reusing your User + bcrypt + Flask-Login ---
# ----- Admin HTML login -----
@app.get("/admin/login")
def admin_login_form():
    if current_user.is_authenticated and is_admin_user():
        return redirect("/admin/")
    return """
    <form method='post' action='/admin/login' style='max-width:340px;margin:60px auto;font-family:system-ui'>
      <h3>Admin login</h3>
      <input name='email' placeholder='Email' style='width:100%;padding:8px;margin:6px 0'>
      <input name='password' type='password' placeholder='Password' style='width:100%;padding:8px;margin:6px 0'>
      <button type='submit' style='padding:8px 12px'>Sign in</button>
      <p style='font-size:12px;color:#666'>Email must match ADMIN_EMAILS</p>
    </form>
    """

@app.post("/admin/login")
def admin_login_submit():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    u = User.query.filter_by(email=email).first()
    if not u or not u.password:
        return "Invalid credentials", 401
    ok = False
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), u.password.encode("utf-8"))
    except Exception:
        ok = False
    if not ok:
        return "Invalid credentials", 401
    login_user(u, remember=True, duration=timedelta(days=90))
    return redirect("/admin/")

@app.post("/admin/logout")
@login_required
def admin_logout():
    logout_user()
    return redirect("/admin/login")

# ----- Admin pages -----
@app.get("/admin/")
@admin_required
def admin_dashboard():
    total_users = db.session.query(func.count(User.id)).scalar()
    total_recipes = db.session.query(func.count(Recipe.id)).scalar()
    active_subs = (db.session.query(func.count(UserSubscription.id))
                   .filter(UserSubscription.status.in_(["active","trial"])).scalar())
    pending = db.session.query(func.count(PendingUser.uuid)).scalar()
    recent_payments = (db.session.query(PaymentTransaction)
                       .order_by(desc(PaymentTransaction.created_at)).limit(10).all())
    BODY = """
    <h2>Dashboard</h2>
    <div class="msg">Total users: <b>{{ total_users }}</b></div>
    <div class="msg">Total recipes: <b>{{ total_recipes }}</b></div>
    <div class="msg">Active/trial subs: <b>{{ active_subs }}</b></div>
    <div class="msg">Pending signups: <b>{{ pending }}</b></div>
    <h3>Recent payments</h3>
    <table>
      <tr><th>ID</th><th>User</th><th>Amount</th><th>Status</th><th>Provider</th><th>When</th></tr>
      {% for p in recent_payments %}
      <tr>
        <td>{{ p.id }}</td>
        <td><a href="{{ url_for('admin_user_detail', user_id=p.user_id) }}">#{{ p.user_id }}</a></td>
        <td>{{ '%.2f'|format(p.amount) }} {{ p.currency }}</td>
        <td>{{ p.status }}</td>
        <td>{{ p.provider }}</td>
        <td>{{ p.created_at }}</td>
      </tr>
      {% endfor %}
    </table>
    """
    return _render_admin(BODY, "Dashboard",
                         total_users=total_users, total_recipes=total_recipes,
                         active_subs=active_subs, pending=pending,
                         recent_payments=recent_payments)

@app.get("/admin/users")
@admin_required
def admin_users_list():
    try: page = max(1, int(request.args.get("page", 1)))
    except: page = 1
    try: per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except: per_page = 50
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "-signup")

    qry = User.query
    if q:
        like = f"%{q}%"
        qry = qry.filter(or_(User.email.ilike(like), User.first_name.ilike(like)))

    if sort == "email":
        qry = qry.order_by(User.email.asc())
    elif sort == "name":
        qry = qry.order_by(User.first_name.asc(), User.email.asc())
    else:
        qry = qry.order_by(User.signup_date.is_(None), User.signup_date.desc())

    rows = qry.limit(per_page + 1).offset((page - 1) * per_page).all()
    has_more = len(rows) > per_page
    users = rows[:per_page]

    subq = (db.session.query(UserSubscription.user_id,
                             func.max(UserSubscription.created_at).label("mx"))
            .group_by(UserSubscription.user_id).subquery())
    latest_subs = {
        s.user_id: s for s in db.session.query(UserSubscription)
        .join(subq, (UserSubscription.user_id == subq.c.user_id) & (UserSubscription.created_at == subq.c.mx))
        .all()
    }
    credits_map = {c.user_id: c for c in UserCredits.query.filter(UserCredits.user_id.in_([u.id for u in users])).all()}

    BODY = """
    <h2>Users</h2>
    <form method="get">
      <input name="q" value="{{ q or '' }}" placeholder="search email or name">
      <select name="sort">
        <option value="-signup" {% if sort=='-signup' %}selected{% endif %}>Newest</option>
        <option value="email" {% if sort=='email' %}selected{% endif %}>Email</option>
        <option value="name" {% if sort=='name' %}selected{% endif %}>Name</option>
      </select>
      <button type="submit">Search</button>
    </form>
    <table>
      <tr><th>ID</th><th>Email</th><th>Name</th><th>Signup</th><th>Plan</th><th>Status</th><th>Text/wk</th><th>Images</th></tr>
      {% for u in users %}
        {% set s = latest_subs.get(u.id) %}
        {% set c = credits_map.get(u.id) %}
        <tr>
          <td><a href="{{ url_for('admin_user_detail', user_id=u.id) }}">{{ u.id }}</a></td>
          <td>{{ u.email }}</td>
          <td>{{ u.first_name or '' }}</td>
          <td>{{ u.signup_date or '' }}</td>
          <td>{{ s.plan_id if s else '' }}</td>
          <td>{{ s.status if s else '' }}</td>
          <td>{{ c.text_remaining_week if c else 0 }}</td>
          <td>{{ c.image_remaining_lifetime if c else 0 }}</td>
        </tr>
      {% endfor %}
    </table>
    <div>
      {% if page>1 %}<a href="?page={{ page-1 }}&per_page={{ per_page }}&q={{ q }}&sort={{ sort }}">Prev</a>{% endif %}
      <span>Page {{ page }}</span>
      {% if has_more %}<a href="?page={{ page+1 }}&per_page={{ per_page }}&q={{ q }}&sort={{ sort }}">Next</a>{% endif %}
    </div>
    """
    return _render_admin(BODY, "Users",
                         users=users, latest_subs=latest_subs, credits_map=credits_map,
                         page=page, per_page=per_page, q=q, sort=sort, has_more=has_more)

@app.get("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id: int):
    u = User.query.get_or_404(user_id)
    subs = (UserSubscription.query.filter_by(user_id=u.id)
            .order_by(UserSubscription.created_at.desc()).all())
    credits = UserCredits.query.get(u.id)
    recipes = (Recipe.query.filter_by(user_id=u.id)
              .order_by(Recipe.created_at.desc()).limit(50).all())
    payments = (PaymentTransaction.query.filter_by(user_id=u.id)
                .order_by(PaymentTransaction.created_at.desc()).all())
    plans = (SubscriptionPlan.query
         .order_by(SubscriptionPlan.period.asc(), SubscriptionPlan.price.asc())
         .all())
    current_sub = (
        UserSubscription.query
        .filter(UserSubscription.user_id == u.id,
                UserSubscription.status.in_(["active", "trial"]))
        .order_by(
            UserSubscription.end_date.is_(None),     # non-nulls first
            UserSubscription.end_date.desc(),
            UserSubscription.start_date.desc(),
        )
        .first()
    ) or (
        UserSubscription.query
        .filter(UserSubscription.user_id == u.id)
        .order_by(
            UserSubscription.end_date.is_(None),
            UserSubscription.end_date.desc(),
            UserSubscription.start_date.desc(),
        )
        .first()
    )

    BODY = """
    <h2>User #{{ u.id }} — {{ u.email }}</h2>
    <p>Name: {{ u.first_name or '' }} | TZ: {{ u.timezone or '' }} | Lang: {{ u.language or '' }} | Audio: {{ 'on' if u.enable_audio else 'off' }}</p>

    {% if request.args.get('msg') %}
      <div class="msg">{{ request.args.get('msg') }}</div>
    {% endif %}

    <h3>Credits</h3>
    <form method="post" action="/admin/users/{{ u.id }}/credits">
      <label>Text remaining this week:
        <input type="number" name="text_remaining_week" value="{{ credits.text_remaining_week if credits else 0 }}" min="0">
      </label>
      <label>Images remaining lifetime:
        <input type="number" name="image_remaining_lifetime" value="{{ credits.image_remaining_lifetime if credits else 0 }}" min="0">
      </label>
      <button type="submit">Update credits</button>
    </form>

    <h3>Subscription</h3>

    {% if not plans %}
      <div class="msg">No plans found. <a href="/admin/plans">Seed default plans</a>.</div>
    {% endif %}

    {% if current_sub %}
      <div class="msg">
        Current: <b>{{ current_sub.plan_id }}</b>
        · status {{ current_sub.status }}
        · start {{ current_sub.start_date }}
        · end {{ current_sub.end_date or '—' }}
        · auto renew {{ 'yes' if current_sub.auto_renew else 'no' }}
      </div>
    {% else %}
      <div class="msg">No subscription on record</div>
    {% endif %}
    <h3></h3>
    <form method="post" action="/admin/users/{{ u.id }}/subscription" class="grid2">
      <label>Action</label>
      <select name="action">
        <option value="create">Create new</option>
        <option value="update_latest">Update latest</option>
      </select>

      <label>Plan</label>
      <select name="plan_id">
        {% for p in plans %}
          <option value="{{ p.id }}"
            {% if current_sub and p.id == current_sub.plan_id %}selected{% endif %}>
            {{ p.id }} ({{ p.period }}, ${{ '%.2f'|format(p.price) }})
          </option>
        {% endfor %}
      </select>

      <label>Status</label>
      <select name="status">
        {% for s in ['active','trial','canceled','expired'] %}
          <option value="{{ s }}"
            {% if current_sub and s == current_sub.status %}selected{% endif %}>{{ s }}</option>
        {% endfor %}
      </select>

      <label>Auto renew</label>
      <select name="auto_renew">
        <option value="0" {% if current_sub and not current_sub.auto_renew %}selected{% endif %}>no</option>
        <option value="1" {% if current_sub and current_sub.auto_renew %}selected{% endif %}>yes</option>
      </select>

      <label>Start (blank = now)</label>
      <input name="start_date" placeholder="YYYY-MM-DD or ISO"
             value="{{ current_sub.start_date if current_sub else '' }}">

      <label>End (blank = auto by plan)</label>
      <input name="end_date" placeholder="YYYY-MM-DD or ISO"
             value="{{ current_sub.end_date if current_sub else '' }}">

      <label>Payment provider</label>
      <select name="payment_provider">
        <option></option><option>apple</option><option>google</option><option>stripe</option>
      </select>

      <label>Payment method</label>
      <input name="payment_method" placeholder="card / apple / google">

      <div></div><button class="btn" type="submit">Save subscription</button>
    </form>

    <h3>Set password</h3>
    <form method="post" action="/admin/users/{{ u.id }}/password">
      <input name="password" type="password" minlength="8" required placeholder="New password">
      <button type="submit">Set password</button>
    </form>

    <h3>Recipes (latest 50)</h3>
    <table>
      <tr><th>ID</th><th>Created</th><th>archived</th><th>Summary</th><th>Text</th><th>Analysis</th><th>Actions</th></tr>
      {% for d in recipes %}
        <tr>
          <td>{{ d.id }}</td>
          <td>{{ d.created_at }}</td>
          <td>{{ d.archived and 'yes' or 'no' }}</td>
          <td>{{ d.title }}</td>
          <td>{{ d.description }}</td>
          <td>{{ d.ingredients }}</td>
          <td>{{ d.instructions }}</td>
          <td>{{ d.categories }}</td>
          <td>
            <form class="inline" method="post" action="/admin/users/{{ u.id }}/recipes/{{ d.id }}/toggle-archived">
              <button type="submit">{{ d.archived and 'Unhide' or 'Hide' }}</button>
            </form>
            <form class="inline" method="post" action="/admin/users/{{ u.id }}/recipes/{{ d.id }}/delete" onsubmit="return confirm('Delete recipe {{ d.id }}?');">
              <button type="submit">Delete</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </table>

    <h3>Subscriptions (history)</h3>
    <table>
      <tr><th>ID</th><th>Plan</th><th>Status</th><th>Start</th><th>End</th><th>Auto</th><th>Provider</th></tr>
      {% for s in subs %}
        <tr><td>{{ s.id }}</td><td>{{ s.plan_id }}</td><td>{{ s.status }}</td><td>{{ s.start_date }}</td><td>{{ s.end_date or '' }}</td><td>{{ 'yes' if s.auto_renew else 'no' }}</td><td>{{ s.payment_provider or '' }}</td></tr>
      {% endfor %}
    </table>

    <h3>Payments</h3>
    <table>
      <tr><th>ID</th><th>Amount</th><th>Status</th><th>Provider</th><th>Txn</th><th>When</th></tr>
      {% for p in payments %}
        <tr><td>{{ p.id }}</td><td>{{ '%.2f'|format(p.amount) }} {{ p.currency }}</td><td>{{ p.status }}</td><td>{{ p.provider }}</td><td>{{ p.provider_transaction_id or '' }}</td><td>{{ p.created_at }}</td></tr>
      {% endfor %}
    </table>
    """
    return _render_admin(BODY, f"User {u.id}",
                         u=u, subs=subs, credits=credits, recipes=recipes, payments=payments, plans=plans, current_sub=current_sub)

# Optional: debug
@app.get("/admin/debug")
def admin_debug():
    emails = list(_get_admin_emails())
    return {
        "is_authenticated": current_user.is_authenticated,
        "email": (current_user.email or None) if current_user.is_authenticated else None,
        "ADMIN_EMAILS": emails,
        "match": current_user.is_authenticated and (current_user.email or "").lower() in emails,
    }, 200


# --- Helpers ---
def _parse_iso_optional(s: str | None):
    if not s:
        return None
    v = s.strip()
    if not v:
        return None
    try:
        if len(v) == 10:
            return datetime.strptime(v, "%Y-%m-%d")
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        raise ValueError("Invalid date; use YYYY-MM-DD or ISO 8601")

def _admin_redirect(user_id: int, msg: str = ""):
    msg_q = f"?msg={requests.utils.quote(msg)}" if msg else ""
    return redirect(f"/admin/users/{user_id}{msg_q}")

# --- Update credits ---
@app.post("/admin/users/<int:user_id>/credits")
@admin_required
def admin_update_credits(user_id: int):
    u = User.query.get_or_404(user_id)
    try:
        tw = int(request.form.get("text_remaining_week", "0"))
        iw = int(request.form.get("image_remaining_lifetime", "0"))
        if tw < 0 or iw < 0:
            return _admin_redirect(user_id, "Credits must be >= 0")
        uc = UserCredits.query.get(user_id)
        if not uc:
            # week_anchor_utc required; set to start of current week in UTC
            now = datetime.utcnow()
            week_anchor = now - timedelta(days=now.weekday())  # Monday
            uc = UserCredits(user_id=user_id, week_anchor_utc=week_anchor, text_remaining_week=tw, image_remaining_lifetime=iw)
            db.session.add(uc)
        else:
            uc.text_remaining_week = tw
            uc.image_remaining_lifetime = iw
        db.session.commit()
        return _admin_redirect(user_id, "Credits updated")
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to update credits")

# --- Create or update subscription ---
@app.post("/admin/users/<int:user_id>/subscription")
@admin_required
def admin_update_subscription(user_id: int):
    u = User.query.get_or_404(user_id)
    action = (request.form.get("action") or "create").strip()
    plan_id = (request.form.get("plan_id") or "").strip()
    status = (request.form.get("status") or "active").strip()
    auto_renew = (request.form.get("auto_renew") or "0").strip() in ("1", "true", "yes")
    payment_provider = (request.form.get("payment_provider") or "").strip() or None
    payment_method = (request.form.get("payment_method") or "").strip() or None
    start_date = request.form.get("start_date") or ""
    end_date = request.form.get("end_date") or ""

    plan = SubscriptionPlan.query.get(plan_id) if plan_id else None
    if not plan:
        return _admin_redirect(user_id, "Invalid plan")

    try:
        sd = _parse_iso_optional(start_date) or datetime.utcnow()
        ed = _parse_iso_optional(end_date)
        if not ed:
            # compute by plan.period
            if (plan.period or "").lower().startswith("month"):
                ed = sd + relativedelta(months=1)
            elif (plan.period or "").lower().startswith("year"):
                ed = sd + relativedelta(years=1)
            else:
                ed = sd + timedelta(days=30)

        if action == "update_latest":
            latest = (UserSubscription.query
                      .filter_by(user_id=user_id)
                      .order_by(UserSubscription.created_at.desc()).first())
            if not latest:
                return _admin_redirect(user_id, "No subscription to update")
            latest.plan_id = plan_id
            latest.status = status
            latest.start_date = sd
            latest.end_date = ed
            latest.auto_renew = auto_renew
            latest.payment_provider = payment_provider
            latest.payment_method = payment_method
            db.session.commit()
            return _admin_redirect(user_id, "Subscription updated")
        else:
            sub = UserSubscription(
                user_id=user_id,
                plan_id=plan_id,
                status=status,
                start_date=sd,
                end_date=ed,
                auto_renew=auto_renew,
                payment_provider=payment_provider,
                payment_method=payment_method
            )
            db.session.add(sub)
            db.session.commit()
            return _admin_redirect(user_id, "Subscription created")
    except ValueError as ve:
        db.session.rollback()
        return _admin_redirect(user_id, str(ve))
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to save subscription")

# --- Toggle recipe archived ---
@app.post("/admin/users/<int:user_id>/recipes/<int:recipe_id>/toggle-archived")
@admin_required
def admin_toggle_recipe_archived(user_id: int, recipe_id: int):
    d = Recipe.query.get_or_404(recipe_id)
    if d.user_id != user_id:
        return _admin_redirect(user_id, "Recipe does not belong to user")
    try:
        d.archived = not bool(d.archived)
        db.session.commit()
        return _admin_redirect(user_id, f"Recipe {recipe_id} {'archived' if d.archived else 'unarchived'}")
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to toggle")


@app.post("/admin/users/<int:user_id>/recipes/<int:recipe_id>/delete")
@admin_required
def admin_delete_recipe(user_id: int, recipe_id: int):
    d = Recipe.query.get_or_404(recipe_id)
    if d.user_id != user_id:
        return _admin_redirect(user_id, "Recipr does not belong to user")
    try:
        db.session.delete(d)
        db.session.commit()
        return _admin_redirect(user_id, f"Recipe {recipe_id} deleted")
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to delete recipe")

# --- Set user password ---
@app.post("/admin/users/<int:user_id>/password")
@admin_required
def admin_set_password(user_id: int):
    u = User.query.get_or_404(user_id)
    pw = request.form.get("password") or ""
    if len(pw) < 8:
        return _admin_redirect(user_id, "Password must be at least 8 chars")
    try:
        u.password = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        db.session.commit()
        return _admin_redirect(user_id, "Password updated")
    except Exception:
        db.session.rollback()
        return _admin_redirect(user_id, "Failed to update password")



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)

