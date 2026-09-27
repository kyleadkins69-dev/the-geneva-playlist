# The Geneva Playlist

Private Discord radio bot: rules reaction verification, approved-song library, priority requests, and automatic shuffled playback.

## Important music caveat

This prototype uses `yt-dlp` to resolve YouTube links for playback. Automated extraction/retransmission may violate YouTube's terms and may stop working at any time. Only use audio you have permission to play; replace the extractor with a licensed/permitted source before relying on it. The playlist stores URLs and metadata, not audio files. It does not bypass DRM or download a permanent audio collection.

## What is already configured

- Server ID: `1553750425562521690`
- Rules message ID: `1553773581601603758`
- Under Investigation role ID: `1553751430475677709`
- Co-Conspirators role ID: `1553750989717246055`

**You still need the rules CHANNEL ID.** Right-click the channel containing the rules embed with Discord Developer Mode enabled and copy its ID. Set `RULES_CHANNEL_ID` in `.env` (when `0`, matching is only by guild and message ID). Make sure the bot can see that channel, read message history and add reactions. Add a **✅** reaction to the existing rules message yourself; members must click that same reaction.

## Discord setup

1. Developer Portal → Bot → enable **Server Members Intent**. No Message Content intent is needed.
2. Bot role needs **Manage Roles**, View Channels, Read Message History, Add Reactions, Send Messages, Connect, Speak. Move its role above **Under Investigation** in Server Settings → Roles.
3. For the rules channel, allow new members to view the rules and add reactions. Keep public and gaming channels hidden from `@everyone` and visible to **Under Investigation** and any higher tiers as appropriate. This project does not modify channel permissions.
4. Slash commands sync to your specified server when the bot starts.

## Run on your computer for an initial test

Install Python 3.12+ and FFmpeg, then:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env  # Windows: copy .env.example .env
# Edit .env with your bot token and rules channel ID.
# The bot reads your .env automatically.
python bot.py
```

**Easier:** Use Docker Compose, which installs FFmpeg and dependencies automatically:

```bash
cp .env.example .env
# Edit .env with your token and channel ID
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

The `.env` is deliberately excluded from Git. Never paste your bot token into GitHub or chat.

## Free online hosting: Oracle Cloud Always Free VM

Always Free capacity and account eligibility are not guaranteed. Oracle may require card verification, available VM shapes vary by region, and inactive instances may be reclaimed. Set billing alerts and stay within the Always Free limits.

1. Create an Oracle Cloud account. In Compute → Instances, create an **Always Free eligible** Ubuntu instance, selecting a free-eligible shape and boot volume. If none is available in your region, you may need to wait or choose another provider.
2. Add your SSH public key when creating the instance. Connect with the SSH instructions shown in Oracle's console. Do **not** expose a public HTTP port for this bot; it connects outbound to Discord.
3. On the VM, install Docker Engine and Docker Compose plugin following Docker's official Ubuntu instructions: https://docs.docker.com/engine/install/ubuntu/
4. Create a **private** GitHub repository, upload these project files (not `.env` or `data`), and clone it on the VM. For a private repo, use a GitHub deploy key or GitHub CLI authentication. Alternatively, upload the project ZIP directly via `scp`.
5. On the VM, run `cp .env.example .env`, edit `.env` with your token and rules channel ID, then `mkdir -p data && docker compose up -d --build`.
6. Check `docker compose logs -f`. Your bot should come online. After changing code: `git pull && docker compose up -d --build`.

**Persistence:** The playlist database is saved to `./data` on the VM. Back up that directory periodically. A VM deletion or disk loss can otherwise erase it.

## Commands

Everyone verified: `/play` (start shuffled radio), `/play url:<youtube link>` (queue next), `/skip`, `/pause`, `/resume`, `/queue`, `/nowplaying`, `/leave`, `/playlist view`, `/playlist play`, `/playlist shuffle`.

Co-Conspirators only: `/playlist add` (current track), `/playlist add url:<link>`, `/playlist remove song_id:<number>`, `/playlist clear confirm:true`.

A requested track plays after the current track. Multiple requests play in arrival order. Once requests finish, radio resumes randomised library playback. Tracks are shuffled in cycles to minimise repeats.

## Limitations

- The bot is designed for one small server and one voice connection at a time.
- It does not automatically join a voice channel when nobody requests music; use `/play` to start it.
- YouTube URLs may fail or become unavailable. Free hosting does not guarantee 24/7 uptime.
- Use `/playlist view` to find IDs for removal.
