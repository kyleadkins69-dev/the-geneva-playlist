"""The Geneva Playlist: Discord verification and a small shared radio bot."""
import asyncio
import logging
import os
import random
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger('geneva')
TOKEN = os.environ['DISCORD_TOKEN']
GUILD_ID = int(os.getenv('GUILD_ID', '1553750425562521690'))
RULES_MESSAGE_ID = int(os.getenv('RULES_MESSAGE_ID', '1553773581601603758'))
RULES_CHANNEL_ID = int(os.getenv('RULES_CHANNEL_ID', '0'))
VERIFIED_ROLE_ID = int(os.getenv('VERIFIED_ROLE_ID', '1553751430475677709'))
ADMIN_ROLE_ID = int(os.getenv('ADMIN_ROLE_ID', '1553750989717246055'))
DB_PATH = os.getenv('DB_PATH', 'data/playlist.sqlite3')
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(DB_PATH)
DB.row_factory = sqlite3.Row
DB.execute('CREATE TABLE IF NOT EXISTS songs (id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL, added_by INTEGER NOT NULL, added_at TEXT DEFAULT CURRENT_TIMESTAMP)')
DB.commit()

intents = discord.Intents.default()
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
guild_object = discord.Object(id=GUILD_ID)

@dataclass
class Track:
    url: str
    title: str
    requester: str = 'Radio'

@dataclass
class Radio:
    requests: deque = field(default_factory=deque)
    bag: list = field(default_factory=list)
    current: Track | None = None
    active: bool = False
    text_channel: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

radios: dict[int, Radio] = {}

def radio(guild_id):
    return radios.setdefault(guild_id, Radio())

def approved():
    return [Track(row['url'], row['title']) for row in DB.execute('SELECT url,title FROM songs ORDER BY id')]

def is_admin(member):
    return isinstance(member, discord.Member) and any(role.id == ADMIN_ROLE_ID for role in member.roles)

def is_verified(member):
    return isinstance(member, discord.Member) and (is_admin(member) or any(role.id == VERIFIED_ROLE_ID for role in member.roles) or member.guild_permissions.administrator)

async def guard(interaction, *, admin=False, voice=False):
    if not interaction.guild or interaction.guild.id != GUILD_ID:
        await interaction.response.send_message('This command is only available in our server.', ephemeral=True)
        return False
    if admin and not is_admin(interaction.user):
        await interaction.response.send_message('Only The Co-Conspirators can edit the permanent playlist.', ephemeral=True)
        return False
    if not admin and not is_verified(interaction.user):
        await interaction.response.send_message('React to the rules first to unlock the bot.', ephemeral=True)
        return False
    if voice:
        member_voice = getattr(interaction.user, 'voice', None)
        if not member_voice or not member_voice.channel:
            await interaction.response.send_message('Join a voice channel first.', ephemeral=True)
            return False
        vc = interaction.guild.voice_client
        if vc and vc.channel != member_voice.channel:
            await interaction.response.send_message('Join the same voice channel as the bot.', ephemeral=True)
            return False
    return True

YDL_META = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'extract_flat': False, 'skip_download': True, 'format': 'bestaudio/best'}

def extract(url):
    if not url.startswith(('https://www.youtube.com/watch?', 'https://youtu.be/', 'https://music.youtube.com/watch?')):
        raise ValueError('Please use a single YouTube video URL (not a playlist).')
    with yt_dlp.YoutubeDL(YDL_META) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get('_type') == 'playlist' or not info.get('id'):
        raise ValueError('Could not resolve a single video.')
    return Track('https://www.youtube.com/watch?v=' + info['id'], info.get('title') or 'Untitled'), info.get('url')

async def lookup(url):
    return await asyncio.to_thread(extract, url)

def next_track(state):
    if state.requests:
        return state.requests.popleft()
    library = approved()
    if not library:
        return None
    urls = {t.url for t in library}
    state.bag = [t for t in state.bag if t.url in urls]
    if not state.bag:
        state.bag = library
        random.shuffle(state.bag)
        if state.current and len(state.bag) > 1 and state.bag[-1].url == state.current.url:
            state.bag[0], state.bag[-1] = state.bag[-1], state.bag[0]
    return state.bag.pop()

async def advance(guild):
    state = radio(guild.id)
    async with state.lock:
        vc = guild.voice_client
        if not state.active or not vc or not vc.is_connected() or vc.is_playing() or vc.is_paused():
            return
        while state.active:
            track = next_track(state)
            if track is None:
                state.current = None
                if state.text_channel:
                    await state.text_channel.send('The approved library is empty. Add a song with `/playlist add` or request one with `/play`.')
                return
            try:
                _, stream = await lookup(track.url)
                if not stream:
                    raise RuntimeError('No playable audio stream was returned.')
                source = discord.FFmpegPCMAudio(stream, before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', options='-vn')
                state.current = track
                loop = asyncio.get_running_loop()
                def finished(error):
                    if error:
                        LOG.error('Playback failed: %s', error)
                    asyncio.run_coroutine_threadsafe(advance(guild), loop)
                vc.play(source, after=finished)
                if state.text_channel:
                    await state.text_channel.send(f'Now playing: **{discord.utils.escape_markdown(track.title)}** ({track.requester})')
                return
            except Exception as exc:
                LOG.warning('Cannot play %s: %s', track.url, exc)
                if state.text_channel:
                    await state.text_channel.send(f'Could not play **{discord.utils.escape_markdown(track.title)}**. Trying the next song.')
                # Avoid endlessly retrying an entirely broken library.
                if not state.requests and not state.bag:
                    state.active = False
                    return

@bot.event
async def on_ready():
    LOG.info('Logged in as %s', bot.user)

@bot.event
async def setup_hook():
    await bot.tree.sync(guild=guild_object)

@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id != GUILD_ID or payload.message_id != RULES_MESSAGE_ID or str(payload.emoji) not in ('✅', '☑️'):
        return
    if RULES_CHANNEL_ID and payload.channel_id != RULES_CHANNEL_ID:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    member = payload.member or guild.get_member(payload.user_id)
    if not member:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    if member.bot:
        return
    role = guild.get_role(VERIFIED_ROLE_ID)
    if role and role not in member.roles:
        try:
            await member.add_roles(role, reason='Accepted server rules')
        except discord.Forbidden:
            LOG.error('Cannot assign verified role: check Manage Roles and role hierarchy.')

@bot.tree.command(name='play', description='Start shuffled radio or queue a YouTube URL next.', guild=guild_object)
@app_commands.describe(url='Optional single YouTube video URL')
async def play(interaction: discord.Interaction, url: str | None = None):
    if not await guard(interaction, voice=True):
        return
    await interaction.response.defer()
    state = radio(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        vc = await interaction.user.voice.channel.connect()
    state.text_channel = interaction.channel
    state.active = True
    if url:
        try:
            track, _ = await lookup(url)
        except Exception as exc:
            await interaction.followup.send(f'Could not read that URL: {exc}')
            return
        track.requester = interaction.user.display_name
        state.requests.append(track)
        await interaction.followup.send(f'Queued next: **{discord.utils.escape_markdown(track.title)}**')
    else:
        await interaction.followup.send('Radio started. Shuffling the approved library.')
    if not vc.is_playing() and not vc.is_paused():
        await advance(interaction.guild)

@bot.tree.command(name='skip', description='Skip to the next request or shuffled track.', guild=guild_object)
async def skip(interaction: discord.Interaction):
    if not await guard(interaction, voice=True): return
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message('Nothing is playing.', ephemeral=True)
        return
    await interaction.response.send_message('Skipped.')
    vc.stop()

@bot.tree.command(name='pause', description='Pause playback.', guild=guild_object)
async def pause(interaction: discord.Interaction):
    if not await guard(interaction, voice=True): return
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message('Paused.')
    else:
        await interaction.response.send_message('Nothing to pause.', ephemeral=True)

@bot.tree.command(name='resume', description='Resume playback.', guild=guild_object)
async def resume(interaction: discord.Interaction):
    if not await guard(interaction, voice=True): return
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message('Resumed.')
    else:
        await interaction.response.send_message('Nothing is paused.', ephemeral=True)

@bot.tree.command(name='leave', description='Stop the radio and leave voice.', guild=guild_object)
async def leave(interaction: discord.Interaction):
    if not await guard(interaction, voice=True): return
    state = radio(interaction.guild.id)
    state.active = False
    state.requests.clear()
    state.current = None
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect(force=True)
    await interaction.response.send_message('Radio off. The evidence has been sealed.')

@bot.tree.command(name='queue', description='Show upcoming requested tracks.', guild=guild_object)
async def queue(interaction: discord.Interaction):
    if not await guard(interaction): return
    state = radio(interaction.guild.id)
    upcoming = list(state.requests)[:15]
    lines = [f'{i}. {discord.utils.escape_markdown(t.title)} — {t.requester}' for i,t in enumerate(upcoming, 1)]
    await interaction.response.send_message('\n'.join(lines) if lines else 'No requests queued. Radio will continue shuffling.', ephemeral=True)

@bot.tree.command(name='nowplaying', description='Show the current track.', guild=guild_object)
async def nowplaying(interaction: discord.Interaction):
    if not await guard(interaction): return
    t = radio(interaction.guild.id).current
    await interaction.response.send_message(f'Now playing: **{discord.utils.escape_markdown(t.title)}** — {t.url}' if t else 'Nothing playing.', ephemeral=True)

playlist = app_commands.Group(name='playlist', description='Permanent approved song library', guild_ids=[GUILD_ID])

@playlist.command(name='view', description='View approved songs.')
async def playlist_view(interaction: discord.Interaction):
    if not await guard(interaction): return
    rows = DB.execute('SELECT id,title FROM songs ORDER BY id').fetchall()
    if not rows:
        await interaction.response.send_message('No approved songs yet.', ephemeral=True)
        return
    chunks = []
    current = ''
    for row in rows:
        line = f"`{row['id']}` {discord.utils.escape_markdown(row['title'])}\n"
        if len(current) + len(line) > 1800:
            chunks.append(current)
            current = ''
        current += line[:1800]
    if current: chunks.append(current)
    await interaction.response.send_message(f'**Approved songs ({len(rows)})**\n' + chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)

@playlist.command(name='add', description='Co-Conspirators: save a YouTube URL or the current track.')
@app_commands.describe(url='Leave blank to save the current song')
async def playlist_add(interaction: discord.Interaction, url: str | None = None):
    if not await guard(interaction, admin=True): return
    await interaction.response.defer(ephemeral=True)
    if not url:
        current = radio(interaction.guild.id).current
        if not current:
            await interaction.followup.send('Nothing playing. Supply a YouTube URL.')
            return
        track = current
    else:
        try:
            track, _ = await lookup(url)
        except Exception as exc:
            await interaction.followup.send(f'Could not read that URL: {exc}')
            return
    try:
        DB.execute('INSERT INTO songs(url,title,added_by) VALUES(?,?,?)', (track.url,track.title,interaction.user.id))
        DB.commit()
        await interaction.followup.send(f'Approved and saved: **{discord.utils.escape_markdown(track.title)}**')
    except sqlite3.IntegrityError:
        await interaction.followup.send('That song is already in the approved library.')

@playlist.command(name='remove', description='Co-Conspirators: remove a song by its library ID.')
async def playlist_remove(interaction: discord.Interaction, song_id: int):
    if not await guard(interaction, admin=True): return
    row = DB.execute('SELECT title FROM songs WHERE id=?', (song_id,)).fetchone()
    if not row:
        await interaction.response.send_message('Song ID not found.', ephemeral=True)
        return
    DB.execute('DELETE FROM songs WHERE id=?', (song_id,))
    DB.commit()
    radio(interaction.guild.id).bag = [t for t in radio(interaction.guild.id).bag if t.title != row['title']]
    await interaction.response.send_message(f"Removed: **{discord.utils.escape_markdown(row['title'])}**")

@playlist.command(name='clear', description='Co-Conspirators: clear the entire permanent library.')
async def playlist_clear(interaction: discord.Interaction, confirm: bool = False):
    if not await guard(interaction, admin=True): return
    if not confirm:
        await interaction.response.send_message('To delete every approved song, run `/playlist clear confirm:true`.', ephemeral=True)
        return
    DB.execute('DELETE FROM songs')
    DB.commit()
    radio(interaction.guild.id).bag.clear()
    await interaction.response.send_message('The permanent library has been cleared.')

@playlist.command(name='play', description='Start the approved radio in your voice channel.')
async def playlist_play(interaction: discord.Interaction):
    await play.callback(interaction, None)

@playlist.command(name='shuffle', description='Reshuffle upcoming radio tracks.')
async def playlist_shuffle(interaction: discord.Interaction):
    if not await guard(interaction): return
    state = radio(interaction.guild.id)
    state.bag.clear()
    await interaction.response.send_message('The upcoming radio selection has been reshuffled.')

bot.tree.add_command(playlist, guild=guild_object)

@bot.tree.error
async def on_app_command_error(interaction, error):
    LOG.exception('Slash command failed: %s', error)
    msg = 'Something went wrong. Check the bot logs.'
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

bot.run(TOKEN)
