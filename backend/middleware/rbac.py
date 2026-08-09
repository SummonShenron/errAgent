# backend/middleware/rbac.py
from fastapi import HTTPException, Depends, status
from backend.utils.isolation_auth import get_current_user
from backend.utils.db_utils import get_db

def require_role(required_group: str):
    """FastAPI dependency factory for group-based access control."""
    def role_checker(user: dict = Depends(get_current_user)):
        username = user.get("username")
        db = get_db()
        
        # Query directory user record
        user_record = db["directory"].find_one({"clerk_id": username}) or {}
        user_groups = user_record.get("groups", [])
        
        if required_group not in user_groups and "Global_Admins" not in user_groups:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Requires '{required_group}' authority."
            )
        return user

    return role_checker