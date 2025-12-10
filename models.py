from pydantic import BaseModel

class LoginRequest(BaseModel):
    email: str  # ← Changed from EmailStr
    password: str

class LoginResponse(BaseModel):
    status: str
    token: str | None = None
    expires_in: int | None = None
    message: str | None = None
