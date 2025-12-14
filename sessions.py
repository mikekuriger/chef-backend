# In-memory session store (use Redis or a DB for production)
user_sessions = {}

def get_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    return user_sessions[user_id]

def add_to_session(user_id, role, content):
    get_session(user_id).append({"role": role, "content": content})

def reset_session(user_id):
    user_sessions[user_id] = []

