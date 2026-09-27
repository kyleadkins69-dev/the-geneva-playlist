"""The Geneva Playlist: Discord-backed approved library and shuffled radio."""
import asyncio
import json
import logging
import os
import random
from collections import deque
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger('geneva')
TOKEN = os.environ['DISCORD_TOKEN']
GUILD_ID = int(os.getenv('GUILD_ID', '1553750425562521690'))
RULES_MESSAGE_ID = int(os.getenv('RULES_MESSAGE_ID', '1553773581601603758'))
RULES_CHANNEL_ID = int(os.getenv('RULES_CHANNEL_ID', '1553771482235281489'))
VERIFIED_ROLE_ID = int(os.getenv('VERIFIED_ROLE_ID', '1553751430475677709'))
ADMIN_ROLE_ID = int(os.getenv('ADMIN_ROLE_ID', '1553750989717246055'))
MUSIC_DB_CHANNEL_ID = int(os.getenv('MUSIC_DB_CHANNEL_ID', '1553793756724076675'))
GUILD = discord.Object(id=GUILD_ID)

intents = discord.Intents.default()
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

@dataclass
class Track:
    url: str
    title: str
    requester: str = 'Radio'
    record_id: int | None = None

@dataclass
class Radio:
    requests: deque = field(default_factory=deque)
    bag: list = field(default_factory=list)
    current: Track | None = None
    active: bool = False
    text_channel: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    playback_generation: int = 0

state = Radio()
# Only Discord messages are durable. This in-memory map is rebuilt at startup.
library: dict[int, Track] = {}
library_lock = asyncio.Lock()
YDL_OPTIONS = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
               'skip_download': True, 'format': 'bestaudio/best'}


def approved():
    return list(library.values())


def is_admin(member):
    return isinstance(member, discord.Member) and (member.id == member.guild.owner_id or any(r.id == ADMIN_ROLE_ID for r in member.roles))


def is_verified(member):
    if not isinstance(member, discord.Member):
        return False
    if is_admin(member) or member.guild_permissions.administrator:
        return True
    verified = member.guild.get_role(VERIFIED_ROLE_ID)
    return verified is not None and any(r.position >= verified.position for r in member.roles)


async def guard(interaction, *, admin=False, voice=False):
    if not interaction.guild or interaction.guild.id != GUILD_ID:
        await interaction.response.send_message('This command only works in our server.', ephemeral=True)
        return False
    if admin and not is_admin(interaction.user):
        await interaction.response.send_message('Only The Co-Conspirators can change the approved playlist.', ephemeral=True)
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


def extract(url):
    if not url.startswith(('https://www.youtube.com/watch?', 'https://youtu.be/',
                           'https://music.youtube.com/watch?')):
        raise ValueError('Supply a single YouTube video URL, not a playlist.')
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get('_type') == 'playlist' or not info.get('id'):
        raise ValueError('Could not resolve a single video.')
    return (Track('https://www.youtube.com/watch?v=' + info['id'],
                  info.get('title') or 'Untitled'), info.get('url'))


async def lookup(url):
    return await asyncio.to_thread(extract, url)


async def database_channel():
    channel = bot.get_channel(MUSIC_DB_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(MUSIC_DB_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD_ID:
        raise RuntimeError('MUSIC_DB_CHANNEL_ID must be a text channel in this server.')
    return channel


def decode_record(message):
    if message.author.id != bot.user.id or not message.content.startswith('GENEVA_SONG_V1\n'):
        return None
    try:
        obj = json.loads(message.content.split('\n', 1)[1])
        if not isinstance(obj['url'], str) or not isinstance(obj['title'], str):
            return None
        if not obj['url'].startswith('https://www.youtube.com/watch?v='):
            return None
        return Track(obj['url'], obj['title'], record_id=message.id)
    except (ValueError, KeyError, TypeError):
        return None


async def reload_library():
    channel = await database_channel()
    loaded = {}
    async for message in channel.history(limit=None, oldest_first=True):
        track = decode_record(message)
        if track:
            loaded[message.id] = track
    async with library_lock:
        library.clear()
        library.update(loaded)
        state.bag.clear()
    LOG.info('Loaded %d approved songs from Discord', len(library))


def next_track():
    if state.requests:
        return state.requests.popleft()
    songs = approved()
    if not songs:
        return None
    valid_urls = {t.url for t in songs}
    state.bag = [t for t in state.bag if t.url in valid_urls]
    if not state.bag:
        state.bag = songs[:]
        random.shuffle(state.bag)
        if state.current and len(state.bag) > 1 and state.bag[-1].url == state.current.url:
            state.bag[0], state.bag[-1] = state.bag[-1], state.bag[0]
    return state.bag.pop()


async def advance(guild):
    async with state.lock:
        vc = guild.voice_client
        if not state.active or not vc or not vc.is_connected() or vc.is_playing() or vc.is_paused():
            return
        # Each song can be tried once per advance; broken links cannot cause an endless loop.
        attempts = max(len(library) + len(state.requests), 1)
        for _ in range(attempts):
            track = next_track()
            if track is None:
                state.current = None
                if state.text_channel:
                    await state.text_channel.send('No approved songs yet. A Co-Conspirator can use `/playlist add`.')
                return
            try:
                _, stream = await lookup(track.url)
                if not stream:
                    raise RuntimeError('No audio stream returned')
                source = discord.FFmpegPCMAudio(
                    stream,
                    before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                    options='-vn')
                state.current = track
                state.playback_generation += 1
                generation = state.playback_generation
                loop = asyncio.get_running_loop()

                def finished(error):
                    if error:
                        LOG.warning('Playback error: %s', error)
                    async def continue_radio():
                        if generation == state.playback_generation:
                            await advance(guild)
                    asyncio.run_coroutine_threadsafe(continue_radio(), loop)

                vc.play(source, after=finished)
                if state.text_channel:
                    await state.text_channel.send(
                        f'Now playing: **{discord.utils.escape_markdown(track.title)}** ({discord.utils.escape_markdown(track.requester)})')
                return
            except Exception as exc:
                LOG.warning('Cannot play %s: %s', track.url, exc)
        state.current = None
        state.active = False
        if state.text_channel:
            await state.text_channel.send('Could not play any available tracks. Check the bot logs and audio source.')


@bot.event
async def setup_hook():
    await bot.tree.sync(guild=GUILD)


@bot.event
async def on_ready():
    LOG.info('Logged in as %s', bot.user)
    if not getattr(bot, '_library_loaded', False):
        try:
            await reload_library()
            bot._library_loaded = True
        except (discord.HTTPException, RuntimeError) as exc:
            LOG.error('Cannot load music database: %s', exc)


@bot.event
async def on_raw_reaction_add(payload):
    if (payload.guild_id != GUILD_ID or payload.channel_id != RULES_CHANNEL_ID or
            payload.message_id != RULES_MESSAGE_ID or str(payload.emoji) != '✅'):
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    member = payload.member or guild.get_member(payload.user_id)
    if member is None:
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
            LOG.error('Cannot assign role; check Manage Roles and bot role hierarchy')


@bot.tree.command(name='play', description='Start random radio, or queue a YouTube URL next.', guild=GUILD)
async def play(interaction: discord.Interaction, url: str | None = None):
    if not await guard(interaction, voice=True):
        return
    await interaction.response.defer()
    if url:
        try:
            track, _ = await lookup(url)
        except Exception as exc:
            await interaction.followup.send(f'Could not read that link: {exc}')
            return
        track.requester = interaction.user.display_name
        state.requests.append(track)
    vc = interaction.guild.voice_client
    if not vc:
        try:
            vc = await interaction.user.voice.channel.connect()
        except (discord.ClientException, discord.HTTPException) as exc:
            await interaction.followup.send(f'Could not join voice: {exc}')
            return
    state.text_channel = interaction.channel
    state.active = True
    await interaction.followup.send(
        f'Queued next: **{discord.utils.escape_markdown(track.title)}**' if url
        else 'Radio started. Playing the approved library in random order.')
    if not vc.is_playing() and not vc.is_paused():
        await advance(interaction.guild)


@bot.tree.command(name='skip', description='Skip to the next request or radio track.', guild=GUILD)
async def skip(interaction: discord.Interaction):
    if not await guard(interaction, voice=True): return
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message('Nothing is playing.', ephemeral=True)
        return
    await interaction.response.send_message('Skipped.')
    vc.stop()


@bot.tree.command(name='pause', description='Pause playback.', guild=GUILD)
async def pause(interaction: discord.Interaction):
    if not await guard(interaction, voice=True): return
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message('Paused.')
    else:
        await interaction.response.send_message('Nothing to pause.', ephemeral=True)


@bot.tree.command(name='resume', description='Resume playback.', guild=GUILD)
async def resume(interaction: discord.Interaction):
    if not await guard(interaction, voice=True): return
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message('Resumed.')
    else:
        await interaction.response.send_message('Nothing is paused.', ephemeral=True)


@bot.tree.command(name='leave', description='Stop the radio and disconnect.', guild=GUILD)
async def leave(interaction: discord.Interaction):
    if not await guard(interaction, voice=True): return
    state.active = False
    state.playback_generation += 1
    state.requests.clear()
    state.current = None
    state.bag.clear()
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect(force=True)
    await interaction.response.send_message('Radio off. The evidence has been sealed.')


@bot.tree.command(name='queue', description='Show upcoming requests.', guild=GUILD)
async def queue(interaction: discord.Interaction):
    if not await guard(interaction): return
    tracks = list(state.requests)[:15]
    lines = [f'{i}. {discord.utils.escape_markdown(t.title)} — {discord.utils.escape_markdown(t.requester)}'
             for i, t in enumerate(tracks, 1)]
    await interaction.response.send_message('\n'.join(lines) if lines else 'No requests queued; radio will shuffle.', ephemeral=True)


@bot.tree.command(name='nowplaying', description='Show the current track.', guild=GUILD)
async def nowplaying(interaction: discord.Interaction):
    if not await guard(interaction): return
    t = state.current
    await interaction.response.send_message(
        f'Now playing: **{discord.utils.escape_markdown(t.title)}** — {t.url}' if t else 'Nothing playing.',
        ephemeral=True)


playlist = app_commands.Group(name='playlist', description='Permanent approved song library', guild_ids=[GUILD_ID])


@playlist.command(name='view', description='View the approved library.')
async def playlist_view(interaction: discord.Interaction):
    if not await guard(interaction): return
    songs = sorted(library.values(), key=lambda t: t.record_id)
    if not songs:
        await interaction.response.send_message('No approved songs yet.', ephemeral=True)
        return
    lines = [f'`{t.record_id}` {discord.utils.escape_markdown(t.title)}' for t in songs]
    chunks, current = [], ''
    for line in lines:
        if len(current) + len(line) + 1 > 1750:
            chunks.append(current)
            current = ''
        current += line + '\n'
    if current: chunks.append(current)
    await interaction.response.send_message(f'**Approved songs ({len(songs)})**\n' + chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@playlist.command(name='add', description='Co-Conspirators: approve a URL or the currently playing song.')
async def playlist_add(interaction: discord.Interaction, url: str | None = None):
    if not await guard(interaction, admin=True): return
    await interaction.response.defer(ephemeral=True)
    if url:
        try:
            track, _ = await lookup(url)
        except Exception as exc:
            await interaction.followup.send(f'Could not read that link: {exc}')
            return
    else:
        track = state.current
        if track is None:
            await interaction.followup.send('Nothing playing. Supply a YouTube URL.')
            return
    try:
        async with library_lock:
            if any(t.url == track.url for t in library.values()):
                await interaction.followup.send('Already approved.')
                return
            channel = await database_channel()
            record = {'url': track.url, 'title': track.title, 'added_by': interaction.user.id}
            msg = await channel.send('GENEVA_SONG_V1\n' + json.dumps(record, ensure_ascii=False))
            library[msg.id] = Track(track.url, track.title, record_id=msg.id)
        await interaction.followup.send(f'Approved and saved: **{discord.utils.escape_markdown(track.title)}**')
    except (discord.HTTPException, RuntimeError) as exc:
        LOG.error('Could not save approved song: %s', exc)
        await interaction.followup.send('Could not save the song. Check music-database channel permissions.')


@playlist.command(name='remove', description='Co-Conspirators: remove a song using its ID from /playlist view.')
async def playlist_remove(interaction: discord.Interaction, song_id: str):
    if not await guard(interaction, admin=True): return
    await interaction.response.defer(ephemeral=True)
    try:
        record_id = int(song_id)
    except ValueError:
        await interaction.followup.send('Use the numeric ID shown by `/playlist view`.')
        return
    try:
        async with library_lock:
            track = library.get(record_id)
            if track is None:
                await interaction.followup.send('Song ID not found.')
                return
            channel = await database_channel()
            await (await channel.fetch_message(record_id)).delete()
            del library[record_id]
            state.bag = [t for t in state.bag if t.record_id != record_id]
        await interaction.followup.send(f'Removed: **{discord.utils.escape_markdown(track.title)}**')
    except (discord.HTTPException, RuntimeError) as exc:
        LOG.error('Could not remove song: %s', exc)
        await interaction.followup.send('Could not delete the record. Check bot permissions.')


@playlist.command(name='clear', description='Co-Conspirators: delete the entire approved library.')
async def playlist_clear(interaction: discord.Interaction, confirm: bool = False):
    if not await guard(interaction, admin=True): return
    if not confirm:
        await interaction.response.send_message('Run `/playlist clear confirm:true` to delete all approved songs.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = 0
    try:
        async with library_lock:
            channel = await database_channel()
            for record_id in list(library):
                try:
                    await (await channel.fetch_message(record_id)).delete()
                except discord.NotFound:
                    pass
                else:
                    deleted += 1
                del library[record_id]
            state.bag.clear()
        await interaction.followup.send(f'Cleared the approved library ({deleted} records deleted).')
    except (discord.HTTPException, RuntimeError) as exc:
        LOG.error('Clear stopped: %s', exc)
        await interaction.followup.send('Deletion stopped partway through. Retry `/playlist clear confirm:true`.')


@playlist.command(name='play', description='Start approved radio in your voice channel.')
async def playlist_play(interaction: discord.Interaction):
    await play.callback(interaction, None)


@playlist.command(name='shuffle', description='Reshuffle upcoming radio tracks.')
async def playlist_shuffle(interaction: discord.Interaction):
    if not await guard(interaction): return
    state.bag.clear()
    await interaction.response.send_message('Upcoming radio tracks reshuffled.')


bot.tree.add_command(playlist, guild=GUILD)


@bot.tree.error
async def on_app_command_error(interaction, error):
    LOG.error('Slash command error: %s', error, exc_info=(type(error), error, error.__traceback__))
    if interaction.response.is_done():
        await interaction.followup.send('Something went wrong. Check the bot console.', ephemeral=True)
    else:
        await interaction.response.send_message('Something went wrong. Check the bot console.', ephemeral=True)


if __name__ == '__main__':
    bot.run(TOKEN)
