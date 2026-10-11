# Runs inside the quay-app container (cwd /quay-registry), fed on stdin by the quay-config role.
# Mints a long-lived OAuth token for the admin user on the "automation" application that
# /api/v1/user/initialize created (its own token lasts 2.5 h: create_user_access_token's default
# expires_in=9000), deletes the user's other tokens on that application, and prints only the new
# token. With QUAY_MINT_DRY_RUN=1 it prints the existing tokens' expiry instead.
import logging
import os
import sys

logging.disable(logging.CRITICAL)

from app import app  # noqa: E402,F401  (initialises Quay's config and database)
from data import model  # noqa: E402

user = model.user.get_user(os.environ["QUAY_ADMIN_USER"])
client_id = os.environ.get("QUAY_TOKEN_CLIENT_ID", user.username)
existing = [t for t in model.oauth.list_access_tokens_for_user(user) if t.application.client_id == client_id]
if os.environ.get("QUAY_MINT_DRY_RUN") == "1":
    print([str(t.expires_at) for t in existing])
    sys.exit(0)
scope = "org:admin repo:admin repo:create repo:read repo:write super:user user:admin user:read"
created, token = model.oauth.create_user_access_token(
    user, client_id, scope, expires_in=int(os.environ.get("QUAY_TOKEN_TTL_DAYS", "365")) * 86400
)
for t in existing:
    t.delete_instance()
sys.stdout.write(token)
