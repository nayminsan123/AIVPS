# Private VPS Chat UI

A private browser chat app using `llama.cpp`, HTTPS from Caddy, username/password accounts, and an admin dashboard. Chat history stays in the browser; users and AI settings persist in a Docker-managed SQLite volume.

## Git safety

The `.gitignore` excludes `.env` files, downloaded model weights, and local database files. Commit `.env.example` and the application source; never commit your real `.env`, model file, or database volume contents.

## Model and 8 GB RAM

The requested Q8_0 GGUF is listed at about 4.48 GB. This deployment uses a 4,096-token context and one inference slot to fit an 8 GB VPS as safely as possible. Actual use also depends on the operating system and CPU. Add 2–4 GB swap as a safety margin; it may prevent an out-of-memory crash but will be slow. If the VPS still runs out of RAM, use the repository's Q4_K_M model (about 2.6 GB) and save it as `models/model.gguf`.

## Deploy on Ubuntu

1. Point a DNS `A` record for the intended hostname to the VPS IPv4 address. Allow inbound TCP 80 and 443 in the VPS firewall/security group.
2. Install Docker Engine and the Compose plugin using Docker's official Ubuntu instructions: <https://docs.docker.com/engine/install/ubuntu/>.
3. Copy this directory to the VPS. In the directory, create the environment file and edit it:

   ```sh
   cp .env.example .env
   chmod 600 .env
   nano .env
   ```

   Set `DOMAIN`, `APP_USERNAME`, and `APP_PASSWORD`. The first account created in an empty database is an administrator. Use a unique password of at least 14 characters. Generate `SESSION_SECRET` with `openssl rand -hex 32`; do not reuse it elsewhere.

4. Download the requested model (about 4.48 GB):

   ```sh
   mkdir -p models
   curl -L --fail --retry 3 \
     'https://huggingface.co/HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive/resolve/main/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf' \
     -o models/model.gguf
   ```

   Check disk space first; keep at least 10 GB free for the model, Docker images, and updates.

5. Start the app:

   ```sh
   docker compose up -d --build
   docker compose logs -f llama
   ```

   When the model is loaded, visit `https://DOMAIN`. Caddy obtains and renews the TLS certificate. The first load can take several minutes.

## Admin dashboard

Sign in with the initial admin account and choose **Admin**.

- **AI settings:** change the system prompt, temperature, top-p, and maximum output tokens. These apply to new requests immediately. Context length stays at 4,096 tokens and requires a server restart to change.
- **Users:** add accounts, select user/admin role, disable or enable accounts, reset passwords, and remove accounts. Passwords must be at least 12 characters. Existing sessions are invalidated when an account is disabled, removed, demoted, or has its password reset. At least one active administrator is always required.
- The first admin credentials are only used when the database is empty. Later changes to `.env` do not change existing account passwords. Use the dashboard to reset them.

## Operations

- View logs: `docker compose logs -f chat llama caddy`.
- Stop services: `docker compose down`. The SQLite database, model, and Caddy certificate data remain in their volumes/folders.
- The model API is not published to the host. Keep public access limited to ports 80/443 and restrict SSH to your own IP where possible.
- Back up the Docker volume named `chat_data` before removing volumes or migrating the VPS; it contains user accounts and AI settings.
- Container image tags currently track upstream releases. For repeatable production updates, pin tested image versions/digests.
