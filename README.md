# ColorDock Token API

FastAPI service that:
- Verifies user credentials from a management repo (live CSV via GitHub API)
- Uses a GitHub App to generate an installation access token
- Issues a short-lived session token per user
- Ensures only one active token per user

## API

### POST /login

Request:

