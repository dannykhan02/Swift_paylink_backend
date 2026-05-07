"""
auth.py
─────────────────────────────────────────────────────────────
JWT authentication for the PayPal admin dashboard.

Registered in app.py via  app.register_blueprint(auth_bp, url_prefix="/api/auth")

Endpoints
─────────
  POST /api/auth/setup          — create the very first admin (no token needed,
                                   only works when admin_users table is empty)
  POST /api/auth/login          — exchange email+password for a JWT
  GET  /api/auth/me             — return current admin profile  [JWT required]
  POST /api/auth/register       — create a second admin         [JWT required]
  POST /api/auth/change-password— change own password           [JWT required]

Token format
────────────
  Authorization: Bearer <token>

  The token payload contains:
    sub   — admin_user.id  (as string)
    email — admin_user.email
    role  — "admin"

  Tokens expire after JWT_ACCESS_TOKEN_EXPIRES (default 8 hours, set in config).

Dependencies to add to requirements.txt
────────────────────────────────────────
  flask-jwt-extended>=4.6
"""

from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    jwt_required,
    get_jwt_identity,
    get_jwt,
)

from models import db, AdminUser

# ── Blueprint ─────────────────────────────────────────────────
auth_bp = Blueprint("auth", __name__)

# ── JWT manager (initialised in app.py via jwt.init_app(app)) ─
jwt = JWTManager()


# ══════════════════════════════════════════════════════════════
#  JWT ERROR HANDLERS
#  These replace Flask-JWT-Extended's default HTML responses
#  with clean JSON so Postman and frontends get consistent
#  error shapes.
# ══════════════════════════════════════════════════════════════

@jwt.unauthorized_loader
def missing_token_callback(reason):
    return jsonify({
        "error":  "Authorization required",
        "detail": reason,
    }), 401


@jwt.invalid_token_loader
def invalid_token_callback(reason):
    return jsonify({
        "error":  "Invalid token",
        "detail": reason,
    }), 401


@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    return jsonify({
        "error":  "Token has expired",
        "detail": "Please log in again",
    }), 401


@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    return jsonify({
        "error":  "Token has been revoked",
        "detail": "Please log in again",
    }), 401


# ══════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════

def _make_token(admin: AdminUser) -> str:
    """Issue a JWT for the given admin."""
    return create_access_token(
        identity=str(admin.id),
        additional_claims={
            "email": admin.email,
            "role":  "admin",
        },
    )


def _admin_from_identity() -> AdminUser | None:
    """Return the AdminUser whose id matches the JWT identity."""
    admin_id = get_jwt_identity()
    return AdminUser.query.get(int(admin_id))


# ══════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════

# ── POST /api/auth/setup ─────────────────────────────────────
@auth_bp.route("/setup", methods=["POST"])
def setup():
    """
    Bootstrap: create the very first admin account.

    This endpoint is only available when the admin_users table
    is completely empty.  Once an admin exists it returns 403.

    Body (JSON):
        email     — required
        password  — required (min 8 chars)
        full_name — optional

    Response 201:
        { "message": "Admin created", "admin": {...}, "access_token": "..." }
    """
    # Block if any admin already exists
    if AdminUser.query.first():
        return jsonify({
            "error": "Setup already complete. Use /api/auth/login instead.",
        }), 403

    data      = request.get_json(force=True) or {}
    email     = (data.get("email") or "").strip().lower()
    password  = data.get("password") or ""
    full_name = (data.get("full_name") or "").strip() or None

    # ── Validate ──────────────────────────────────────────────
    if not email:
        return jsonify({"error": "email is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400

    admin = AdminUser(email=email, password=password, full_name=full_name)
    db.session.add(admin)
    db.session.commit()

    token = _make_token(admin)

    return jsonify({
        "message":      "Admin account created successfully",
        "admin":        admin.to_dict(),
        "access_token": token,
    }), 201


# ── POST /api/auth/login ──────────────────────────────────────
@auth_bp.route("/login", methods=["POST"])
def login():
    """
    Exchange email + password for a JWT.

    Body (JSON):
        email    — required
        password — required

    Response 200:
        {
          "access_token": "eyJ...",
          "token_type":   "Bearer",
          "admin":        { id, email, full_name, last_login }
        }
    """
    data     = request.get_json(force=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400

    admin = AdminUser.query.filter_by(email=email).first()

    if not admin or not admin.check_password(password):
        return jsonify({"error": "Invalid email or password"}), 401

    if not admin.is_active:
        return jsonify({"error": "Account is disabled. Contact another admin."}), 403

    # Stamp last_login
    admin.record_login()
    db.session.commit()

    token = _make_token(admin)

    return jsonify({
        "access_token": token,
        "token_type":   "Bearer",
        "expires_in":   "8 hours",
        "admin":        admin.to_dict(),
    }), 200


# ── GET /api/auth/me ──────────────────────────────────────────
@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    """
    Return the profile of the currently authenticated admin.

    Headers:
        Authorization: Bearer <token>

    Response 200:
        { "admin": { id, email, full_name, is_active, last_login } }
    """
    admin = _admin_from_identity()
    if not admin:
        return jsonify({"error": "Admin not found"}), 404

    return jsonify({"admin": admin.to_dict()}), 200


# ── POST /api/auth/register ───────────────────────────────────
@auth_bp.route("/register", methods=["POST"])
@jwt_required()
def register():
    """
    Create a second (or subsequent) admin account.
    Requires an existing admin's JWT token.

    Body (JSON):
        email     — required
        password  — required (min 8 chars)
        full_name — optional

    Response 201:
        { "message": "Admin created", "admin": {...} }
    """
    # Only existing admins can create new admins
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    data      = request.get_json(force=True) or {}
    email     = (data.get("email") or "").strip().lower()
    password  = data.get("password") or ""
    full_name = (data.get("full_name") or "").strip() or None

    if not email:
        return jsonify({"error": "email is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400

    if AdminUser.query.filter_by(email=email).first():
        return jsonify({"error": "An admin with that email already exists"}), 409

    new_admin = AdminUser(email=email, password=password, full_name=full_name)
    db.session.add(new_admin)
    db.session.commit()

    return jsonify({
        "message": "Admin account created successfully",
        "admin":   new_admin.to_dict(),
    }), 201


# ── POST /api/auth/change-password ───────────────────────────
@auth_bp.route("/change-password", methods=["POST"])
@jwt_required()
def change_password():
    """
    Change the currently authenticated admin's password.

    Body (JSON):
        current_password — required
        new_password     — required (min 8 chars)

    Response 200:
        { "message": "Password updated successfully" }
    """
    admin = _admin_from_identity()
    if not admin:
        return jsonify({"error": "Admin not found"}), 404

    data             = request.get_json(force=True) or {}
    current_password = data.get("current_password") or ""
    new_password     = data.get("new_password") or ""

    if not admin.check_password(current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    if len(new_password) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400

    admin.set_password(new_password)
    db.session.commit()

    return jsonify({"message": "Password updated successfully"}), 200