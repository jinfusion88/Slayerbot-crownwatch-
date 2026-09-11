import os
import logging
import sqlite3
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from typing import List, Optional
import datetime
from zoneinfo import ZoneInfo
import re

from db_manager import db, USURP_WARNING_WINS, USURP_WINS_TO_OVERTHROW, DRAW_DAMAGE, build_contender_string, DEFAULT_STARTING_LIFE, BOUNTY_STARTING_LIFE, upkeep_blocks_elapsed, PROVISIONAL_RD, pod_rating_losers, classify_pod_claim, classify_pod_draw, build_draw_pod_ids, promote_draw_to_contested, format_rating_deltas
from moxfield_api import fetch_moxfield_deck, is_moxfield_url, format_color_identity

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)
logger = logging.getLogger('discord')

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

class SlayerBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Slash commands synced globally.")
        
        if not monthly_recap.is_running():
            monthly_recap.start()
        if not life_upkeep_loop.is_running():
            life_upkeep_loop.start()

client = SlayerBot()

# Added to a verification message when it times out.
TIMEOUT_MARKER = "\n\n[⏳ Verification Timed Out]"

# SpellBot pods are always 4 players, anything else gets refused.
SPELLBOT_POD_SIZE = 4
MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
# Discord only allows 25 options in a select menu.
TITLE_SELECT_LIMIT = 25

# Same message /slayed uses, so the context menu says the exact same thing.
SUDDEN_DEATH_REFUSAL = "❌ This title is locked in a Sudden Death sprint! Only the players involved in the draw can claim it."

# SpellBot messages that are currently being logged. Stops someone from
# right clicking the same message twice and logging one game twice.
# Every path that finishes the flow removes the id again.
pending_spellbot_messages: set = set()

# If someone closes the decklist modal with Escape, Discord doesn't tell me,
# so the timeout is what frees the message id. I kept it at 300s like the picker.
SPELLBOT_MODAL_TIMEOUT = 300.0

# Tell the user why they're blocked and that it clears on its own.
DUPLICATE_LOG_WARNING = (
    "⚠️ A match log is already pending for this pod. If nobody completes or "
    f"cancels it, try again in a few minutes (up to {int(SPELLBOT_MODAL_TIMEOUT // 60)} min)."
)

def release_pending_spellbot_message(message_id: Optional[int]) -> None:
    """Removes a message id from the duplicate guard. I use discard so calling
    it twice is fine, and None is ignored.
    """
    if message_id is None:
        return
    pending_spellbot_messages.discard(message_id)

def is_spellbot_message(message: discord.Message) -> bool:
    """True if the message has an embed. I kept this loose on purpose, the real
    check is whether I find exactly 4 players.
    """
    return bool(message.embeds)

def extract_pod_from_message(message: discord.Message) -> List[int]:
    """Gets the player ids out of a SpellBot embed. SpellBot's layout has changed
    between versions, so I check every text spot on the message.
    Returns the ids with no duplicates, in the order they show up.
    """
    surfaces: List[str] = []
    for embed in message.embeds:
        if embed.description:
            surfaces.append(embed.description)
        for field in embed.fields:
            if field.name:
                surfaces.append(field.name)
            if field.value:
                surfaces.append(field.value)
        if embed.footer and embed.footer.text:
            surfaces.append(embed.footer.text)
        if embed.author and embed.author.name:
            surfaces.append(embed.author.name)
    if message.content:
        surfaces.append(message.content)

    # Keep the first order they show up in. I parse raw ids because the bot
    # doesn't have the members intent.
    pod_ids: List[int] = []
    seen = set()
    for text in surfaces:
        for raw_id in MENTION_PATTERN.findall(text):
            user_id = int(raw_id)
            if user_id not in seen:
                seen.add(user_id)
                pod_ids.append(user_id)
    return pod_ids

def sudden_death_blocks(title_data: sqlite3.Row, user_id: int) -> bool:
    """True if the title is in sudden death and this user isn't one of the contenders."""
    sudden_death_str = title_data['sudden_death_contenders']
    if not sudden_death_str:
        return False
    allowed_ids = re.findall(r'\d+', sudden_death_str)
    return str(user_id) not in allowed_ids

def get_member_display_string(user_id: int, interaction: Optional[discord.Interaction] = None) -> str:
    if interaction and interaction.guild:
        member = interaction.guild.get_member(user_id)
        if member:
            return f"**{member.display_name}**"
            
    for guild in client.guilds:
        member = guild.get_member(user_id)
        if member:
            return f"**{member.display_name}**"

    return f"<@{user_id}>"

def format_match_id(match_id: Optional[str]) -> str:
    """Match id line for the receipt so admins can use /undo_match. Empty if there's no id."""
    if not match_id:
        return ""
    return f"\n🆔 Match ID: `{match_id}`"

def clamp_field_value(value: str, limit: int = 1024) -> str:
    """Cuts a field value down to Discord's 1024 limit. discord.py doesn't check
    this itself, so a long value would only fail when sending.
    """
    if len(value) <= limit:
        return value
    return value[:limit - 1] + "…"

AUTOCOMPLETE_POOL = 200  # rows pulled before filtering
AUTOCOMPLETE_CHOICE_LIMIT = 25  # Discord's max autocomplete choices

def title_autocomplete_choices(current: str) -> List[app_commands.Choice[str]]:
    """Title autocomplete. I grab a bigger pool first, filter it by what the user
    typed, and then cut it down to 25, so servers with lots of titles still work.
    """
    try:
        needle = (current or "").strip().lower()
        # Use the stripped needle, otherwise a space would make the LIKE match nothing.
        titles = db.get_titles(needle, limit=AUTOCOMPLETE_POOL)
        matches = [t for t in titles if not needle or needle in t['name'].lower()]
        # Titles that start with what they typed go first, then alphabetical.
        matches.sort(key=lambda t: (not t['name'].lower().startswith(needle), t['name'].lower()))
        return [
            app_commands.Choice(name=t['name'], value=t['name'])
            for t in matches[:AUTOCOMPLETE_CHOICE_LIMIT]
        ]
    except Exception:
        logger.exception("Title autocomplete failed for query %r", current)
        return []

async def build_deck_embed(decklist: Optional[str]) -> tuple:
    """Gets the Moxfield deck info and makes an embed for it.
    Returns (None, None) if it isn't a Moxfield link or the lookup fails,
    and then Discord just shows its normal link preview.
    """
    if not decklist:
        return None, None

    # Everything is in one try so a weird payload just means no embed instead
    # of breaking the title change. Discord's length limits aren't checked by
    # discord.py, so I clamp each text value below.
    try:
        if not is_moxfield_url(decklist):
            return None, None

        metadata = await fetch_moxfield_deck(decklist)
        if not metadata:
            return None, None

        # Discord rejects an empty url, so pass None instead of "".
        embed = discord.Embed(
            title=(metadata["name"] or metadata["commander_name"] or "Decklist")[:256],
            url=metadata["public_url"] or None,
            color=discord.Color.blurple()
        )
        if metadata["commander_name"]:
            embed.add_field(name="Commander", value=metadata["commander_name"][:1024], inline=True)
        embed.add_field(name="Colors", value=format_color_identity(metadata["color_identity"]), inline=True)
        # Only use the image if it looks like a real link.
        if metadata["image_url"].startswith("http"):
            embed.set_thumbnail(url=metadata["image_url"])
        if metadata["author"]:
            embed.set_footer(text=f"Moxfield · {metadata['author']}"[:2048])
        return embed, metadata
    except Exception:
        logger.exception("Moxfield enrichment failed for %s", decklist)
        return None, None

def apply_match_ratings(match_id, winner_id, loser_ids, is_draw=False, deck_metadata=None):
    """Runs the Glicko-2 update for a match. Returns the ratings result, or None
    if anything fails. A rating problem should never break a verification.
    """
    if not match_id:
        return None
    try:
        return db.record_match_ratings(match_id, winner_id, loser_ids,
                                       is_draw=is_draw, deck_metadata=deck_metadata)
    except Exception:
        logger.exception("Rating update failed for match %s", match_id)
        return None

async def expire_view_message(message: Optional[discord.Message], log_label: str):
    """Timeout handler for the verification views. Removes the buttons and adds
    the timed out note once so old prompts can't be clicked forever.
    """
    if not message:
        return
    content = message.content or ""
    if TIMEOUT_MARKER.strip() in content:
        return
    try:
        await message.edit(content=content + TIMEOUT_MARKER, view=None)
    except discord.HTTPException:
        logger.exception(log_label)

async def get_title_or_error(interaction: discord.Interaction, title: str, use_followup: bool = False) -> Optional[sqlite3.Row]:
    """Finds a title by name. If it doesn't exist it sends the error message
    and returns None, so the caller just returns.
    """
    title_data = db.get_title_by_name(title)
    if title_data:
        return title_data

    message = f"❌ Could not find title **{title}**."
    if use_followup:
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return None

def is_admin_override(interaction: discord.Interaction) -> bool:
    """True if the user can verify their own claim (ThunderConductor role or a configured admin role)."""
    if isinstance(interaction.user, discord.Member) and interaction.guild:
        guild_admin_roles = db.get_admin_roles(interaction.guild.id)
        return any(
            role.id == 1226260334306656317 or role.name == "ThunderConductor" or role.id in guild_admin_roles
            for role in interaction.user.roles
        )
    return False

def resolve_announcement_channel(guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
    """The announcement channel for this server, or None if it isn't set up."""
    if not guild:
        return None
    configs = db.get_announcement_channels()
    channel_id = next((c['announcement_channel_id'] for c in configs if c['guild_id'] == guild.id), None)
    if channel_id:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
    return None

@client.event
async def on_ready():
    if client.user:
        logger.info(f'Logged in as {client.user.name} (ID: {client.user.id})')
    else:
        logger.info('Logged in, but client.user is None')
    logger.info('------')
    
    db.setup()

@client.tree.command(name="ping", description="Replies with Pong! Tests if the bot is responsive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

@client.tree.command(name="mint_title", description="Create a new Slayer title.")
@app_commands.default_permissions(administrator=True)
async def mint_title(interaction: discord.Interaction, name: str, role: discord.Role):
    success = db.create_title(name, role.id)
    if success:
        await interaction.response.send_message(f"✅ Successfully minted new title **{name}** linked to role {role.mention}.", ephemeral=False)
    else:
        await interaction.response.send_message(f"❌ A title named **{name}** already exists.", ephemeral=True)

@client.tree.command(name="grant_title", description="Grant a Slayer title to a user.")
@app_commands.default_permissions(administrator=True)
async def grant_title(interaction: discord.Interaction, user: discord.Member, title: str):
    await interaction.response.defer()

    title_data = await get_title_or_error(interaction, title, use_followup=True)
    if not title_data:
        return

    title_id = title_data['id']
    role_id = title_data['discord_role_id']

    current_reign = db.grant_title(title_id, user.id)
    
    if not interaction.guild:
        await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
        return

    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.followup.send(f"⚠️ Title transferred in DB, but the Discord role (ID: {role_id}) could not be found.", ephemeral=False)
        return
        
    try:
        await user.add_roles(role, reason=f"Granted title {title} by {interaction.user}")
        
        if current_reign and current_reign['discord_user_id'] != user.id:
            old_user_id = current_reign['discord_user_id']
            old_member = interaction.guild.get_member(old_user_id)
            if old_member:
                await old_member.remove_roles(role, reason=f"Title {title} transferred to {user}")
                
        await interaction.followup.send(f"🏆 **{user.mention}** has been granted the title **{title}**!")
    except discord.Forbidden:
        await interaction.followup.send(f"✅ Title transferred in DB, but I don't have permission to manage the role {role.name}. Please move my bot role higher in the server settings.", ephemeral=False)
    except discord.HTTPException as e:
        logger.exception("Error granting role")
        await interaction.followup.send(f"✅ Title transferred in DB, but an error occurred assigning the role: {e}", ephemeral=False)

@grant_title.autocomplete('title')
async def grant_title_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="set_announcement_channel", description="Set the channel for bot announcements.")
@app_commands.default_permissions(administrator=True)
async def set_announcement_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return
        
    db.set_announcement_channel(interaction.guild.id, channel.id)
    await interaction.response.send_message(f"✅ Announcement channel set to {channel.mention}.", ephemeral=True)

class DrawSelectView(discord.ui.View):
    def __init__(self, title_id: int, title_name: str, role_id: int, holder_id: int,
                 public_message: Optional[discord.Message] = None,
                 champion_in_pod: bool = True):
        super().__init__(timeout=300)
        self.title_id = title_id
        self.title_name = title_name
        self.role_id = role_id
        self.holder_id = holder_id
        # Was the champion at the table? Only the context menu knows this for sure.
        # /slayed leaves it True. If False, the draw gets rated but doesn't touch the title.
        self.champion_in_pod = champion_in_pod
        # The public message this picker came from. This view updates it when the
        # draw is submitted or puts it back if the picker times out.
        self.public_message = public_message
        self.selected_ids: List[int] = []
        self.submitted = False
        # The picker message, so on_timeout can edit it.
        self.message: Optional[discord.Message] = None

        self.pod_select = discord.ui.UserSelect(
            min_values=1, max_values=4, placeholder="Select your pod members..."
        )
        self.pod_select.callback = self.on_pod_select
        self.add_item(self.pod_select)

        self.confirm_button = discord.ui.Button(
            label="Confirm Draw", style=discord.ButtonStyle.danger, emoji="🤝"
        )
        self.confirm_button.callback = self.on_confirm
        self.add_item(self.confirm_button)

    async def _edit_public_message(self, content: str):
        """Tries to update the public message. If it fails the draw still goes through."""
        if not self.public_message:
            return
        try:
            await self.public_message.edit(content=content, view=None)
        except discord.HTTPException:
            logger.exception("Draw public message edit failed")

    async def on_timeout(self):
        # Already submitted, so don't overwrite it with a timeout message.
        if self.submitted:
            return

        await self._edit_public_message(
            "🤝 A draw was started but never completed — the pod was never submitted, so no damage was applied."
        )

        await expire_view_message(self.message, "Draw picker message timeout cleanup failed")

    def _effective_champion_in_pod(self) -> bool:
        """Checks the pod as it is right now, since the user can still edit the pod list.
        If they add the champion back in, it becomes a contested draw.
        """
        return promote_draw_to_contested(self.champion_in_pod, self.holder_id, self.selected_ids)

    async def on_pod_select(self, interaction: discord.Interaction):
        # Just saving the selection here, no database writes.
        self.selected_ids = [user.id for user in self.pod_select.values]

        names = ", ".join(get_member_display_string(uid, interaction) for uid in self.selected_ids)
        content = f"🤝 Pod selected: {names}. Press **Confirm Draw** to record it."
        # Keep the no damage warning if the champion still isn't in the pod.
        if not self._effective_champion_in_pod():
            content += (
                f"\nChampion <@{self.holder_id}> still isn't in this pod, so the **{self.title_name}** "
                f"takes no damage — this will be recorded and rated as a pod draw only."
            )
        await interaction.response.edit_message(content=content, view=self)

    async def on_confirm(self, interaction: discord.Interaction):
        if self.submitted:
            await interaction.response.send_message(
                "❌ This draw is already being recorded.", ephemeral=True
            )
            return

        # The dropdown lets you confirm with nothing picked, so I check.
        if not self.selected_ids:
            await interaction.response.send_message(
                "❌ Select at least one pod member before confirming.", ephemeral=True
            )
            return

        # Set this before any await so a double click can't post two prompts.
        self.submitted = True
        for child in self.children:
            child.disabled = True

        # Defer first so the database calls can't go over Discord's 3 second limit.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            # Nothing changed yet, so let them try again.
            self.submitted = False
            for child in self.children:
                child.disabled = False
            logger.exception("Draw picker defer failed")
            return

        # Work this out once and use it for everything below so it all agrees.
        champion_in_pod = self._effective_champion_in_pod()

        # The title might have been vacated since /slayed (decay, force vacate,
        # an overthrow), so check again.
        if not db.get_active_reign_for_title(self.title_id):
            # Absent champion draws never had damage, so the message is about the vacant title instead.
            vacancy_reason = (
                "there is no champion left to take draw damage" if champion_in_pod
                else "there is no reign left to record this pod draw against"
            )
            try:
                await interaction.edit_original_response(
                    content=f"❌ The **{self.title_name}** is already vacant — {vacancy_reason}.",
                    view=None
                )
            except discord.HTTPException:
                logger.exception("Draw picker vacancy edit failed")
            await self._edit_public_message("🤝 A draw could not be recorded — the title was already vacant.")
            self.stop()
            return

        # Contested draws add the champion back so they can play for their own title
        # in sudden death. If they weren't at the table they stay out so they don't get rated.
        pod_ids = build_draw_pod_ids(self.selected_ids, self.holder_id, champion_in_pod)

        # dict.fromkeys removes duplicates and keeps the order.
        pod_mentions = ", ".join(f"<@{uid}>" for uid in dict.fromkeys(pod_ids))

        verify_view = DrawVerificationView(
            self.title_id, self.title_name, self.role_id, self.holder_id,
            pod_ids, interaction.user.id, champion_in_pod=champion_in_pod
        )

        # No damage on an absent champion draw, but it still needs someone to verify it.
        if champion_in_pod:
            verify_prompt = (
                f"🤝 <@{interaction.user.id}> reports a **draw** for the **{self.title_name}** with {pod_mentions}.\n"
                f"A pod member must verify this before {DRAW_DAMAGE} damage is applied."
            )
        else:
            verify_prompt = (
                f"🤝 <@{interaction.user.id}> reports a **pod draw** with {pod_mentions}.\n"
                f"Champion <@{self.holder_id}> wasn't in this pod, so the **{self.title_name}** takes no damage — "
                f"a pod member must still verify this before the result is recorded and rated."
            )

        # The picker is ephemeral, so the verification has to be a public followup.
        try:
            public_message = await interaction.followup.send(
                verify_prompt,
                view=verify_view, ephemeral=False, wait=True
            )
        except discord.HTTPException:
            # Nothing changed, so let them retry.
            logger.exception("Draw verification send failed")
            self.submitted = False
            for child in self.children:
                child.disabled = False
            try:
                await interaction.edit_original_response(
                    content="❌ The verification message could not be posted, so nothing was recorded. Try confirming again.",
                    view=self
                )
            except discord.HTTPException:
                logger.exception("Draw picker retry edit failed")
            return

        verify_view.message = public_message

        try:
            await interaction.edit_original_response(
                content=f"🤝 Draw submitted for the **{self.title_name}** — a pod member must verify it.",
                view=None
            )
        except discord.HTTPException:
            logger.exception("Draw picker confirmation edit failed")

        # Just "submitted", not verified. Nobody has confirmed it yet.
        await self._edit_public_message("🤝 A draw was submitted for verification.")

        self.stop()

class DrawVerificationView(discord.ui.View):
    """Verification for a draw. A contested draw costs the champion life and can
    start sudden death, so someone else has to confirm it (unless it's an admin).
    Absent champion draws still move ratings, so they need a second person too.
    """

    def __init__(self, title_id: int, title_name: str, role_id: Optional[int],
                 holder_id: int, pod_ids: List[int], submitter_id: int,
                 champion_in_pod: bool = True):
        super().__init__(timeout=300)
        self.title_id = title_id
        self.title_name = title_name
        self.role_id = role_id
        self.holder_id = holder_id
        # Was the champion at the table? If False, verify skips the damage, vacate
        # and sudden death, and only the ratings change.
        self.champion_in_pod = champion_in_pod
        # The pod from DrawSelectView. I don't rebuild it here, otherwise an absent
        # champion could end up getting rated.
        self.pod_ids = pod_ids
        self.submitter_id = submitter_id
        # The public message, so on_timeout can edit it.
        self.message: Optional[discord.Message] = None
        # Set by the first button click before any await, so a second click
        # can't log the match or apply damage twice.
        self.resolved = False

    async def on_timeout(self):
        # Don't mark it timed out if it already went through.
        if self.resolved:
            return
        await expire_view_message(self.message, "Draw verification timeout cleanup failed")

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green, emoji="✅")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = is_admin_override(interaction)

        # The person who submitted the draw can't verify it themselves.
        if interaction.user.id == self.submitter_id and not is_admin:
            await interaction.response.send_message("❌ You cannot verify your own draw!", ephemeral=True)
            return

        if self.resolved:
            await interaction.response.send_message("❌ This draw is already being processed.", ephemeral=True)
            return

        # Claim the view before the defer so a double click can't run this twice.
        self.resolved = True
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            # Nothing changed yet, so release the view.
            self.resolved = False
            logger.exception("Draw verification defer failed")
            return

        # Check again here since the title could have changed while this was waiting.
        # On a contested draw it also has to be the same champion, otherwise the new
        # champion would take damage for a game they weren't in.
        # On an absent champion draw I only care that the title isn't vacant.
        active_reign = db.get_active_reign_for_title(self.title_id)
        if not active_reign or (self.champion_in_pod and active_reign['discord_user_id'] != self.holder_id):
            if self.champion_in_pod:
                stale_notice = (
                    f"❌ The **{self.title_name}** is no longer held by <@{self.holder_id}> — it was vacated "
                    f"or claimed by someone else since this draw was reported, so no damage was applied."
                )
            else:
                # Only happens when the title went vacant.
                stale_notice = (
                    f"❌ The **{self.title_name}** is now vacant — there is no reign left to record this "
                    f"pod draw against, so nothing was logged or rated."
                )
            try:
                await interaction.edit_original_response(
                    content=stale_notice,
                    view=None
                )
            except discord.HTTPException:
                logger.exception("Draw verification vacancy edit failed")
            finally:
                # Nothing changed but the draw is done either way.
                self.stop()
            return

        # Log the match right before changing anything so undo has the right snapshot.
        # target_id is only the champion if they were at the table, since undo uses it
        # for the rivalry rollback.
        target_id = self.holder_id if self.champion_in_pod else None
        match_id = db.log_match("draw", self.title_id, self.submitter_id, target_id=target_id)

        # Title changes only happen on a contested draw. Ratings happen either way
        # since the four players really did draw.
        new_life = None
        vacated = False

        if self.champion_in_pod:
            # Add the champion to sudden death so they can still win their title back.
            contenders_str = build_contender_string(self.pod_ids, self.submitter_id)

            new_life = db.apply_combat_damage(self.title_id)

            if new_life <= 0:
                # The damage is already saved, so nothing below can be allowed to crash
                # before stop() runs, or the buttons would stay live.
                try:
                    db.vacate_title(self.title_id, is_bounty=False, contenders_list=contenders_str)
                    vacated = True

                    if interaction.guild and self.role_id:
                        member = interaction.guild.get_member(self.holder_id)
                        role = interaction.guild.get_role(self.role_id)
                        if member and role:
                            try:
                                await member.remove_roles(role, reason="Bled out from a draw")
                            except discord.HTTPException:
                                logger.exception("Draw role remove failed")
                except Exception:
                    logger.exception("Draw vacate failed for match %s", match_id)

        # Rate the draw before the announcement so the rating changes can go in it.
        # If rating fails, receipt stays empty and the announcement goes out normally.
        rating_result = None
        receipt = ""
        try:
            rating_result = apply_match_ratings(match_id, None, self.pod_ids, is_draw=True)

            # Inside the try so if formatting fails it just gets logged.
            if rating_result:
                receipt = format_rating_deltas(rating_result["participants"], is_draw=True)
        except Exception:
            logger.exception("Draw rating tail failed for match %s", match_id)

        if not self.champion_in_pod:
            # Only name the champion if they still hold the title.
            absent_note = (
                f"<@{self.holder_id}> wasn't in this pod"
                if active_reign['discord_user_id'] == self.holder_id
                else "the champion at the time wasn't in this pod"
            )
            msg = (
                f"🤝 Pod draw recorded — {absent_note}, so the "
                f"**{self.title_name}** takes no damage."
            )
            if receipt:
                msg += f"\n{receipt}"
            msg += format_match_id(match_id)
            try:
                await interaction.followup.send(msg, ephemeral=False)
            except discord.HTTPException:
                logger.exception("Absent-champion draw report send failed")
        elif new_life <= 0:
            # If the vacate failed, don't announce it.
            if vacated:
                try:
                    announcement_channel = resolve_announcement_channel(interaction.guild)
                    msg = f"🩸 The champion bled out from a draw! The **{self.title_name}** is vacated and locked in a Sudden Death Sprint between the pod!"
                    if receipt:
                        msg += f"\n{receipt}"
                    msg += format_match_id(match_id)

                    try:
                        if announcement_channel and isinstance(announcement_channel, discord.TextChannel):
                            await announcement_channel.send(msg)
                        elif isinstance(interaction.channel, discord.TextChannel):
                            await interaction.channel.send(msg)
                    except discord.HTTPException:
                        logger.exception("Draw announcement send failed")
                except Exception:
                    logger.exception("Draw vacate/announce failed for match %s", match_id)
        else:
            msg = f"🩸 The champion took {DRAW_DAMAGE} damage from the draw! Current Life: {new_life}"
            if receipt:
                msg += f"\n{receipt}"
            msg += format_match_id(match_id)
            try:
                await interaction.followup.send(msg, ephemeral=False)
            except discord.HTTPException:
                logger.exception("Draw damage report send failed")

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        # Show when an admin verified their own draw.
        admin_tag = " **(Admin Override)**" if is_admin and interaction.user.id == self.submitter_id else ""
        # Already saved and announced, so stop() runs even if this edit fails.
        try:
            await interaction.edit_original_response(
                content=f"🤝 This draw has been verified by {interaction.user.mention}.{admin_tag}{format_match_id(match_id)}",
                view=self
            )
        except discord.HTTPException:
            logger.exception("Draw verification confirmation edit failed")
        finally:
            self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = is_admin_override(interaction)

        if interaction.user.id == self.submitter_id and not is_admin:
            await interaction.response.send_message("❌ You cannot deny your own draw!", ephemeral=True)
            return

        # Don't let a deny overwrite a verify that already went through.
        if self.resolved:
            await interaction.response.send_message("❌ This draw is already being processed.", ephemeral=True)
            return

        self.resolved = True
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        # Show when an admin denied their own draw.
        admin_tag = " **(Admin Override)**" if is_admin and interaction.user.id == self.submitter_id else ""
        try:
            await interaction.response.edit_message(
                content=f"❌ This draw was denied by {interaction.user.mention}.{admin_tag}", view=self
            )
        except discord.HTTPException:
            # Denying changes nothing, so if the edit failed let them try again.
            self.resolved = False
            logger.exception("Draw denial edit failed")
            return
        self.stop()

class VerificationView(discord.ui.View):
    def __init__(self, command_author_id: int, title_data: dict, old_holder_id: Optional[int], is_defense: bool, decklist: Optional[str] = None,
                 pod_ids: Optional[List[int]] = None):
        super().__init__(timeout=300)
        self.command_author_id = command_author_id
        self.title_data = title_data
        self.old_holder_id = old_holder_id
        self.is_defense = is_defense
        self.decklist = decklist
        # The full pod if we have it (context menu). /slayed doesn't, so it rates
        # just the claimant vs the old holder.
        self.pod_ids = pod_ids or []
        # The public message, so on_timeout can edit it.
        self.message: Optional[discord.Message] = None
        # Set before the first await so a double click can't log the match twice.
        self.resolved = False

    async def on_timeout(self):
        # Don't mark it timed out if it already went through.
        if self.resolved:
            return
        await expire_view_message(self.message, "Verification timeout cleanup failed")

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green, emoji="✅")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = is_admin_override(interaction)

        if interaction.user.id == self.command_author_id and not is_admin:
            await interaction.response.send_message("❌ You cannot verify your own claim/defense!", ephemeral=True)
            return

        if self.resolved:
            await interaction.response.send_message("❌ This claim/defense is already being processed.", ephemeral=True)
            return

        # Claim the view before the defer.
        self.resolved = True
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            # Nothing changed yet, so release the view.
            self.resolved = False
            logger.exception("Verification defer failed")
            return

        title_id = self.title_data['id']
        title_name = self.title_data['name']
        role_id = self.title_data['discord_role_id']

        announcement_channel = resolve_announcement_channel(interaction.guild)

        admin_tag = " **(Admin Override)**" if is_admin and interaction.user.id == self.command_author_id else ""

        # Moxfield lookup goes after the defer since it can take up to 5 seconds.
        deck_embed, deck_metadata = await build_deck_embed(self.decklist)

        if self.is_defense:
            # Log right before the change so undo has a snapshot.
            match_id = db.log_match("defense", title_id, self.command_author_id)
            # Reset life first so the momentum bonus gets added on top.
            db.lifelink_reset(title_id)
            defense_result = db.log_defense(title_id)
            msg = f"⚡ <@{self.command_author_id}> has successfully defended the **{title_name}**! (Verified by {interaction.user.mention}){admin_tag}"
            if defense_result and defense_result["bonus_applied"]:
                streak = defense_result["defense_streak"]
                msg += f"\n🔥 Momentum Bonus! +5 Life (now on a {streak}-defense streak)"
            wiped = db.wipe_contenders(title_id)
            if wiped:
                msg += f"\n🧹 The champion's defense wipes the contender board! {wiped} usurper streak{'s' if wiped != 1 else ''} reset to 0."
        else:
            # Log right before the change so undo has a snapshot.
            match_id = db.log_match("claim", title_id, self.command_author_id, target_id=self.old_holder_id)
            db.grant_title(title_id, self.command_author_id, decklist=self.decklist)
            db.wipe_contenders(title_id)

            if self.old_holder_id:
                db.record_rivalry_win(self.command_author_id, self.old_holder_id)

            if interaction.guild and role_id:
                role = interaction.guild.get_role(role_id)
                if role:
                    old_member = interaction.guild.get_member(self.old_holder_id) if self.old_holder_id else None
                    new_member = interaction.guild.get_member(self.command_author_id)
                    try:
                        if old_member:
                            await old_member.remove_roles(role, reason=f"Title claimed by {self.command_author_id}")
                        if new_member:
                            await new_member.add_roles(role, reason=f"Claimed title from {self.old_holder_id}")
                    except discord.HTTPException:
                        logger.exception("Role swap failed")

            if self.old_holder_id:
                msg = f"⚡ A new TC Slayer has claimed the title! <@{self.command_author_id}> has taken the **{title_name}** from <@{self.old_holder_id}>! (Verified by {interaction.user.mention}){admin_tag}"
            else:
                msg = f"⚡ A new TC Slayer has claimed the vacant title! <@{self.command_author_id}> has taken the **{title_name}**! (Verified by {interaction.user.mention}){admin_tag}"

        if self.decklist:
            msg += f"\n**Decklist**: {self.decklist}"

        # Rate before the announcement so the rating changes can go in it.
        # If it fails, receipt stays empty.
        rating_result = None
        receipt = ""
        try:
            # The winner is always command_author_id, only the losers change.
            # If there's no real list of opponents I skip rating instead of making one up.
            if self.is_defense:
                # Defense with a pod (context menu) gets rated. A plain /slayed defense doesn't know who they beat.
                rating_losers = (
                    pod_rating_losers(self.pod_ids, self.command_author_id)
                    if self.pod_ids else None
                )
            elif self.pod_ids:
                # Pod claim: everyone else in the pod lost.
                rating_losers = pod_rating_losers(self.pod_ids, self.command_author_id)
            elif self.old_holder_id:
                # /slayed claim: the only known loser is the old champion.
                rating_losers = [self.old_holder_id]
            else:
                # Vacant title and no pod, nobody to rate against.
                rating_losers = None

            if rating_losers:
                rating_decks = None
                if deck_metadata:
                    # Only the claimant's deck is known.
                    rating_decks = {
                        self.command_author_id: {
                            "commander_name": deck_metadata["commander_name"],
                            "color_identity": deck_metadata["color_identity"],
                            "deck_url": deck_metadata["public_url"],
                        }
                    }
                rating_result = apply_match_ratings(match_id, self.command_author_id, rating_losers,
                                                    deck_metadata=rating_decks)
            # Inside the try so if formatting fails it just gets logged.
            if rating_result:
                receipt = format_rating_deltas(rating_result["participants"])
        except Exception:
            logger.exception("Rating tail failed for match %s", match_id)

        if receipt:
            msg += f"\n{receipt}"

        msg += format_match_id(match_id)

        # Only pass embed if there is one. I use "is not None" because an empty
        # Embed counts as falsy.
        send_kwargs = {"embed": deck_embed} if deck_embed is not None else {}

        try:
            if announcement_channel and isinstance(announcement_channel, discord.TextChannel):
                await announcement_channel.send(msg, **send_kwargs)
            elif isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(msg, **send_kwargs)
        except discord.HTTPException:
            logger.exception("Verification announcement send failed")

        # Save the deck info. This is after the announcement and guarded on its own.
        if match_id and deck_metadata:
            try:
                db.set_match_deck_metadata(match_id, deck_metadata["commander_name"],
                                           deck_metadata["color_identity"],
                                           deck_metadata["public_url"])
            except Exception:
                logger.exception("Deck metadata write failed for match %s", match_id)

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        # Already saved and announced, so stop() runs even if this edit fails.
        try:
            await interaction.edit_original_response(content=f"✅ This action has been verified.{format_match_id(match_id)}", view=self)
        except discord.HTTPException:
            logger.exception("Verification confirmation edit failed")
        finally:
            self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = is_admin_override(interaction)

        if interaction.user.id == self.command_author_id and not is_admin:
            await interaction.response.send_message("❌ You cannot deny your own claim/defense!", ephemeral=True)
            return

        # Don't let a deny overwrite a verify that already went through.
        if self.resolved:
            await interaction.response.send_message("❌ This claim/defense is already being processed.", ephemeral=True)
            return

        self.resolved = True
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        admin_tag = " **(Admin Override)**" if is_admin and interaction.user.id == self.command_author_id else ""
        try:
            await interaction.response.edit_message(content=f"❌ This claim/defense was denied by {interaction.user.mention}.{admin_tag}", view=self)
        except discord.HTTPException:
            # Denying changes nothing, so if the edit failed let them try again.
            self.resolved = False
            logger.exception("Verification denial edit failed")
            return
        self.stop()

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.gray, emoji="🤝")
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.old_holder_id and not self.is_defense:
             await interaction.response.send_message("❌ Cannot draw for a vacant title claim.", ephemeral=True)
             return

        # Draw hands this off to DrawSelectView, so it counts as resolved.
        if self.resolved:
            await interaction.response.send_message("❌ This claim/defense is already being processed.", ephemeral=True)
            return

        self.resolved = True

        holder_id = self.command_author_id if self.is_defense else self.old_holder_id

        draw_view = DrawSelectView(
            self.title_data['id'],
            self.title_data['name'],
            self.title_data['discord_role_id'],
            holder_id,
            public_message=interaction.message
        )
        try:
            await interaction.response.send_message("🤝 Select the pod members involved in the draw:", view=draw_view, ephemeral=True)
        except discord.HTTPException:
            # The picker never showed up, so give the verification back.
            self.resolved = False
            logger.exception("Draw picker send failed")
            return
        try:
            draw_view.message = await interaction.original_response()
        except discord.HTTPException:
            logger.exception("Draw picker message capture failed")

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        # Edit this public message, not the new picker. It only says pending,
        # DrawSelectView updates it later.
        if interaction.message:
            try:
                await interaction.message.edit(content="🤝 A draw is being recorded…", view=self)
            except discord.HTTPException:
                logger.exception("Draw pending message edit failed")
        self.stop()

class UsurpVerificationView(discord.ui.View):
    """Verification for /usurp. Same self verify rules as VerificationView, but a win
    here adds a contender win instead of changing the title right away.
    """

    def __init__(self, command_author_id: int, title_data, champion_id: int,
                 pod_ids: Optional[List[int]] = None, decklist: Optional[str] = None):
        super().__init__(timeout=300)
        self.command_author_id = command_author_id
        self.title_data = title_data
        self.champion_id = champion_id
        # The full pod if we have it (context menu). A plain /usurp doesn't, so
        # nothing gets rated. The champion is never rated since they weren't there.
        self.pod_ids = pod_ids or []
        self.decklist = decklist
        # The public message, so on_timeout can edit it.
        self.message: Optional[discord.Message] = None
        # Same double click protection as VerificationView.
        self.resolved = False

    async def on_timeout(self):
        # Don't mark it timed out if it already went through.
        if self.resolved:
            return
        await expire_view_message(self.message, "Usurp verification timeout cleanup failed")

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green, emoji="✅")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = is_admin_override(interaction)

        if interaction.user.id == self.command_author_id and not is_admin:
            await interaction.response.send_message("❌ You cannot verify your own usurper win!", ephemeral=True)
            return

        if self.resolved:
            await interaction.response.send_message("❌ This usurper win is already being processed.", ephemeral=True)
            return

        # Claim the view before the defer.
        self.resolved = True
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            # Nothing changed yet, so release the view.
            self.resolved = False
            logger.exception("Usurp verification defer failed")
            return

        title_id = self.title_data['id']
        title_name = self.title_data['name']
        role_id = self.title_data['discord_role_id']

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # If the champion changed since /usurp, cancel the win.
        active_reign = db.get_active_reign_for_title(title_id)
        if not active_reign or active_reign['discord_user_id'] != self.champion_id:
            try:
                await interaction.edit_original_response(
                    content=f"❌ The **{title_name}** throne has changed since this win was logged — this usurper win is void.",
                    view=self
                )
            except discord.HTTPException:
                logger.exception("Usurp void notice edit failed")
            finally:
                self.stop()
            return

        # Moxfield lookup goes after the defer and after the check above.
        deck_embed, deck_metadata = await build_deck_embed(self.decklist)

        # Log right before the change. A cancelled win doesn't get logged.
        match_id = db.log_match("usurp", title_id, self.command_author_id, target_id=self.champion_id)

        wins = db.add_contender_win(title_id, self.command_author_id)
        admin_tag = " **(Admin Override)**" if is_admin and interaction.user.id == self.command_author_id else ""
        announcement_channel = resolve_announcement_channel(interaction.guild)

        # Only the 4th win actually takes the title. Every verified win with a pod
        # still gets rated either way.
        overthrown = wins >= USURP_WINS_TO_OVERTHROW

        if overthrown:
            db.execute_overthrow(title_id, self.command_author_id)

            if interaction.guild and role_id:
                role = interaction.guild.get_role(role_id)
                if role:
                    old_member = interaction.guild.get_member(self.champion_id)
                    new_member = interaction.guild.get_member(self.command_author_id)
                    try:
                        if old_member:
                            await old_member.remove_roles(role, reason=f"Title usurped by {self.command_author_id}")
                        if new_member:
                            await new_member.add_roles(role, reason=f"Usurped title from {self.champion_id}")
                    except discord.HTTPException:
                        logger.exception("Usurp role swap failed")

            msg = (
                f"👑⚔️ **THE THRONE HAS FALLEN!** ⚔️👑\n"
                f"<@{self.command_author_id}> has **USURPED** the **{title_name}** from <@{self.champion_id}> "
                f"with {USURP_WINS_TO_OVERTHROW} contender wins! The old reign has been archived, the new champion "
                f"is crowned at {BOUNTY_STARTING_LIFE} Life, and all other contender streaks are wiped clean. "
                f"ALL HAIL THE USURPER! (Verified by {interaction.user.mention}){admin_tag}"
            )
        elif wins == USURP_WARNING_WINS:
            msg = (
                f"🚨 **USURPER ALERT** 🚨\n"
                f"<@{self.command_author_id}> is now at **{wins}/{USURP_WINS_TO_OVERTHROW} contender wins** on the "
                f"**{title_name}**!\n"
                f"<@{self.champion_id}> — your throne is **ONE GAME** from falling! Log a defense with `/slayed` "
                f"to wipe the contender board before it's too late! (Verified by {interaction.user.mention}){admin_tag}"
            )
        else:
            msg = (
                f"🗡️ <@{self.command_author_id}> logged a contender win on the **{title_name}** "
                f"({wins}/{USURP_WINS_TO_OVERTHROW} wins toward the overthrow). "
                f"(Verified by {interaction.user.mention}){admin_tag}"
            )

        if self.decklist:
            msg += f"\n**Decklist**: {self.decklist}"

        # Rate before the announcement so the rating changes can go in it.
        # If it fails, receipt stays empty.
        rating_result = None
        receipt = ""
        try:
            # Never rate the old champion here, they weren't in the game.
            # Only the rest of the pod counts as losers.
            rating_losers = pod_rating_losers(self.pod_ids, self.command_author_id)
            if rating_losers:
                rating_decks = None
                if deck_metadata:
                    # Only the usurper's deck is known.
                    rating_decks = {
                        self.command_author_id: {
                            "commander_name": deck_metadata["commander_name"],
                            "color_identity": deck_metadata["color_identity"],
                            "deck_url": deck_metadata["public_url"],
                        }
                    }
                rating_result = apply_match_ratings(match_id, self.command_author_id, rating_losers,
                                                    deck_metadata=rating_decks)
            # Inside the try so if formatting fails it just gets logged.
            if rating_result:
                receipt = format_rating_deltas(rating_result["participants"])
        except Exception:
            logger.exception("Usurp rating tail failed for match %s", match_id)

        if receipt:
            msg += f"\n{receipt}"

        msg += format_match_id(match_id)

        # Only pass embed if there is one. I use "is not None" because an empty
        # Embed counts as falsy.
        send_kwargs = {"embed": deck_embed} if deck_embed is not None else {}

        # The win is already saved, so stop() runs even if this edit fails.
        try:
            await interaction.edit_original_response(content=f"✅ This usurper win has been verified.{format_match_id(match_id)}", view=self)
        except discord.HTTPException:
            logger.exception("Usurp confirmation edit failed")
        finally:
            self.stop()

        try:
            if announcement_channel:
                await announcement_channel.send(msg, **send_kwargs)
            elif isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(msg, **send_kwargs)
        except discord.HTTPException:
            logger.exception("Usurp announcement send failed")

        # Save the deck info. This is after the announcement and guarded on its own.
        if match_id and deck_metadata:
            try:
                db.set_match_deck_metadata(match_id, deck_metadata["commander_name"],
                                           deck_metadata["color_identity"],
                                           deck_metadata["public_url"])
            except Exception:
                logger.exception("Deck metadata write failed for match %s", match_id)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = is_admin_override(interaction)

        if interaction.user.id == self.command_author_id and not is_admin:
            await interaction.response.send_message("❌ You cannot deny your own usurper win!", ephemeral=True)
            return

        # Don't let a deny overwrite a verify that already went through.
        if self.resolved:
            await interaction.response.send_message("❌ This usurper win is already being processed.", ephemeral=True)
            return

        self.resolved = True
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        admin_tag = " **(Admin Override)**" if is_admin and interaction.user.id == self.command_author_id else ""
        try:
            await interaction.response.edit_message(content=f"❌ This usurper win was denied by {interaction.user.mention}.{admin_tag}", view=self)
        except discord.HTTPException:
            # Denying changes nothing, so if the edit failed let them try again.
            self.resolved = False
            logger.exception("Usurp denial edit failed")
            return
        self.stop()

def build_claim_verification(interaction: discord.Interaction, title_data: sqlite3.Row,
                             decklist: Optional[str] = None,
                             pod_ids: Optional[List[int]] = None) -> tuple:
    """Builds the VerificationView and message text for a claim or defense.
    Used by both /slayed and the context menu so the logic lives in one place.
    It doesn't send anything, the caller does that.
    """
    title_id = title_data['id']
    title_name = title_data['name']
    bounty_msg = f"\n💰 **BOUNTY ACTIVE**: This claim starts with {BOUNTY_STARTING_LIFE} life!" if title_data['bounty_active'] else ""

    active_reign = db.get_active_reign_for_title(title_id)

    if not active_reign:
        view = VerificationView(
            command_author_id=interaction.user.id,
            title_data=title_data,
            old_holder_id=None,
            is_defense=False,
            decklist=decklist,
            pod_ids=pod_ids
        )
        return view, f"⚔️ <@{interaction.user.id}> is claiming the vacant **{title_name}**! Someone must verify this claim.{bounty_msg}"

    current_holder_id = active_reign['discord_user_id']
    is_defense = (interaction.user.id == current_holder_id)

    view = VerificationView(
        command_author_id=interaction.user.id,
        title_data=title_data,
        old_holder_id=current_holder_id,
        is_defense=is_defense,
        decklist=decklist,
        pod_ids=pod_ids
    )

    if is_defense:
        return view, f"🛡️ <@{interaction.user.id}> is defending their **{title_name}** title! Someone must verify this defense."
    return view, f"⚔️ <@{interaction.user.id}> claims to have slayed <@{current_holder_id}> for the **{title_name}**! Someone must verify this victory.{bounty_msg}"

@client.tree.command(name="slayed", description="Claim or defend a Slayer title.")
async def slayed(interaction: discord.Interaction, title: str, decklist: Optional[str] = None):
    title_data = await get_title_or_error(interaction, title)
    if not title_data:
        return

    if sudden_death_blocks(title_data, interaction.user.id):
        await interaction.response.send_message(SUDDEN_DEATH_REFUSAL, ephemeral=True)
        return

    # /slayed doesn't know the pod, so it rates claimant vs old holder only.
    view, content = build_claim_verification(interaction, title_data, decklist=decklist)
    await interaction.response.send_message(content, view=view)

    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        logger.exception("Verification message capture failed")

@slayed.autocomplete('title')
async def slayed_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

class ClaimSelectView(discord.ui.View):
    """The picker for the "Log Slayer Match" context menu. You pick the outcome and
    title, then it goes through the same verification as the slash commands.
    It doesn't write to the database itself.
    """

    def __init__(self, pod_ids: List[int], source_url: Optional[str] = None,
                 decklist: Optional[str] = None,
                 spellbot_message_id: Optional[int] = None):
        super().__init__(timeout=300)
        self.pod_ids = pod_ids
        # The SpellBot message id, so this view can release the duplicate guard when it's done.
        self.spellbot_message_id = spellbot_message_id
        # Link to the SpellBot message so the user can check it's the right pod.
        self.source_url = source_url
        # Deck link from the modal, since context menus can't take parameters.
        self.decklist = decklist
        self.outcome: Optional[str] = None
        self.title_id: Optional[int] = None
        self.title_name: Optional[str] = None
        self.submitted = False
        # The picker message, so on_timeout can edit it.
        self.message: Optional[discord.Message] = None

        self.outcome_select = discord.ui.Select(
            placeholder="What happened?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    # Plain ⚔ without the variation selector, Discord can reject that.
                    label="Title Claim", value="claim", emoji="⚔",
                    description="You won the pod and are claiming/defending a title"
                ),
                discord.SelectOption(
                    label="Draw", value="draw", emoji="🤝",
                    description="The pod drew — the champion takes combat damage"
                ),
            ],
        )
        self.outcome_select.callback = self.on_outcome_select
        self.add_item(self.outcome_select)

        # Ask for one more than the limit so I can tell if some titles got cut off.
        titles = db.get_titles(limit=TITLE_SELECT_LIMIT + 1)
        self.titles_truncated = len(titles) > TITLE_SELECT_LIMIT
        visible_titles = titles[:TITLE_SELECT_LIMIT]
        # Just for display, on_confirm reads the title from the database again.
        self.title_names = {t['id']: t['name'] for t in visible_titles}

        self.title_select = discord.ui.Select(
            placeholder="Which title was contested?",
            min_values=1,
            max_values=1,
            # Discord's max label length is 100.
            options=[
                discord.SelectOption(label=t['name'][:100], value=str(t['id']))
                for t in visible_titles
            ],
        )
        self.title_select.callback = self.on_title_select
        self.add_item(self.title_select)

        self.confirm_button = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.primary)
        self.confirm_button.callback = self.on_confirm
        self.add_item(self.confirm_button)

    def render_summary(self, interaction: discord.Interaction) -> str:
        """The picker text showing the pod and what's been picked so far."""
        pod = ", ".join(get_member_display_string(uid, interaction) for uid in self.pod_ids)
        lines = [f"⚔️ **Log a Slayer match** for this pod: {pod}"]

        if self.outcome == "claim":
            lines.append("Outcome: **Title Claim**")
        elif self.outcome == "draw":
            lines.append("Outcome: **Draw**")
        else:
            lines.append("Outcome: _not picked yet_")

        if self.title_name:
            lines.append(f"Title: **{self.title_name}**")
        else:
            lines.append("Title: _not picked yet_")

        if self.titles_truncated:
            lines.append(
                f"⚠️ Only the first {TITLE_SELECT_LIMIT} titles fit in the picker — "
                f"use `/slayed` directly for any other title."
            )
        if self.source_url:
            lines.append(f"Source: {self.source_url}")
        lines.append("Press **Confirm** once both are set.")
        return "\n".join(lines)

    async def on_timeout(self):
        # Release first, in case on_confirm crashed after setting submitted.
        release_pending_spellbot_message(self.spellbot_message_id)
        # Already submitted, so don't mark it timed out.
        if self.submitted:
            return
        await expire_view_message(self.message, "Claim picker message timeout cleanup failed")

    async def on_outcome_select(self, interaction: discord.Interaction):
        # Just saving the selection.
        self.outcome = self.outcome_select.values[0]
        await interaction.response.edit_message(content=self.render_summary(interaction), view=self)

    async def on_title_select(self, interaction: discord.Interaction):
        # Just saving the selection.
        self.title_id = int(self.title_select.values[0])
        self.title_name = self.title_names[self.title_id]
        await interaction.response.edit_message(content=self.render_summary(interaction), view=self)

    async def _finish(self, interaction: discord.Interaction, content: str):
        """Updates the picker message with the final text and stops the view.
        Every way out of on_confirm ends up here, so this is where the
        duplicate guard gets released.
        """
        try:
            await interaction.edit_original_response(content=content, view=None)
        except discord.HTTPException:
            logger.exception("Claim picker confirmation edit failed")
        finally:
            release_pending_spellbot_message(self.spellbot_message_id)
            self.stop()

    async def on_confirm(self, interaction: discord.Interaction):
        if self.submitted:
            await interaction.response.send_message(
                "❌ This match is already being recorded.", ephemeral=True
            )
            return

        # Both dropdowns can be left empty, so I check.
        if self.outcome is None or self.title_id is None:
            await interaction.response.send_message(
                "❌ Pick both an outcome and a title before confirming.", ephemeral=True
            )
            return

        # Set before any await so a double click can't post two prompts.
        self.submitted = True
        for child in self.children:
            child.disabled = True

        # Defer first so the database calls can't go over the 3 second limit.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            # Nothing happened yet, so let them try again.
            self.submitted = False
            for child in self.children:
                child.disabled = False
            logger.exception("Claim picker defer failed")
            return

        # Read the title again in case it was deleted or renamed.
        title_data = db.get_title_by_id(self.title_id)
        if not title_data:
            await self._finish(interaction, "❌ That title no longer exists — nothing was recorded.")
            return

        # Same sudden death check as /slayed.
        if sudden_death_blocks(title_data, interaction.user.id):
            await self._finish(interaction, SUDDEN_DEATH_REFUSAL)
            return

        if self.outcome == "claim":
            # Figure out if the champion was even at the table. If not, this is a usurp.
            active_reign = db.get_active_reign_for_title(title_data['id'])
            champion_id = active_reign['discord_user_id'] if active_reign else None
            claim_kind = classify_pod_claim(champion_id, self.pod_ids, interaction.user.id)

            if claim_kind == "usurp":
                # The person logging a usurp has to be one of the four players.
                if interaction.user.id not in self.pod_ids:
                    await self._finish(
                        interaction,
                        "❌ You weren't one of the 4 players in that pod, so this can't be logged as a "
                        "usurpation. Use `/usurp` if you need to log it manually."
                    )
                    return

                view = UsurpVerificationView(
                    command_author_id=interaction.user.id,
                    title_data=title_data,
                    champion_id=champion_id,
                    pod_ids=self.pod_ids,
                    decklist=self.decklist,
                )
                content = (
                    f"⚡ **Usurpation Claim Detected (Champion not in pod)**\n"
                    f"🗡️ <@{interaction.user.id}> claims a pod win on the **{title_data['name']}** while champion "
                    f"<@{champion_id}> was absent! A pod member must verify this win."
                )
                # The picker is ephemeral, so the verification has to be a public followup.
                try:
                    public_message = await interaction.followup.send(
                        content, view=view, ephemeral=False, wait=True
                    )
                except discord.HTTPException:
                    logger.exception("Usurp verification send failed")
                    await self._finish(
                        interaction,
                        "❌ The verification message could not be posted, so nothing was recorded. Try `/usurp` instead."
                    )
                    return
                view.message = public_message
                await self._finish(
                    interaction,
                    f"⚡ Usurpation verification posted for the **{title_data['name']}** — a pod member must confirm it."
                )
                return

            # vacant, defense, or direct. The pod gets passed along so all 4 players can be rated.
            view, content = build_claim_verification(
                interaction, title_data, pod_ids=self.pod_ids, decklist=self.decklist
            )
            # The picker is ephemeral, so the verification has to be a public followup.
            try:
                public_message = await interaction.followup.send(
                    content, view=view, ephemeral=False, wait=True
                )
            except discord.HTTPException:
                logger.exception("Claim verification send failed")
                await self._finish(
                    interaction,
                    "❌ The verification message could not be posted, so nothing was recorded. Try `/slayed` instead."
                )
                return
            view.message = public_message
            await self._finish(
                interaction,
                f"⚔️ Verification posted for the **{title_data['name']}** — a pod member must confirm it."
            )
            return

        # Draw. A vacant title is different from a champion who just wasn't in the pod.
        active_reign = db.get_active_reign_for_title(title_data['id'])
        champion_id = active_reign['discord_user_id'] if active_reign else None
        draw_kind = classify_pod_draw(champion_id, self.pod_ids)

        # No reign to log the draw against.
        if draw_kind == "vacant":
            await self._finish(
                interaction, "❌ That title is vacant — there is no champion to take draw damage."
            )
            return

        holder_id = champion_id

        # Contested draws damage the champion, so they had to be at the table.
        # If the champion was absent, the draw still gets logged and rated, but it
        # doesn't affect the title.
        champion_in_pod = draw_kind == "contested"

        draw_view = DrawSelectView(
            title_data['id'],
            title_data['name'],
            title_data['discord_role_id'],
            holder_id,
            champion_in_pod=champion_in_pod
        )
        # Fill in the pod so they can confirm right away. The champion gets removed
        # because on_confirm adds them back for sudden death.
        draw_view.selected_ids = [uid for uid in self.pod_ids if uid != holder_id]
        # Show the pod in the dropdown too. Sliced to 4 because Discord fails
        # if there are more defaults than max_values.
        draw_view.pod_select.default_values = [
            discord.Object(id=uid) for uid in draw_view.selected_ids[:draw_view.pod_select.max_values]
        ]

        # Warn that picking in the dropdown replaces the whole pod.
        pod_names = ", ".join(
            get_member_display_string(uid, interaction) for uid in draw_view.selected_ids
        )

        # Tell the user up front if the champion won't take damage.
        if champion_in_pod:
            picker_text = (
                f"🤝 Pod already recorded from the SpellBot match ({pod_names}). "
                f"Press **Confirm Draw** to record it, or use the dropdown to replace the pod."
            )
        else:
            picker_text = (
                f"🤝 Pod already recorded from the SpellBot match ({pod_names}). "
                f"Champion <@{holder_id}> wasn't in this pod, so the **{title_data['name']}** takes no "
                f"damage — this is recorded and rated as a pod draw only. "
                f"Press **Confirm Draw** to record it, or use the dropdown to replace the pod."
            )

        # No public message here since this didn't come from /slayed.
        try:
            picker_message = await interaction.followup.send(
                picker_text,
                view=draw_view, ephemeral=True, wait=True
            )
        except discord.HTTPException:
            logger.exception("Draw picker followup send failed")
            await self._finish(
                interaction, "❌ The draw picker could not be opened, so nothing was recorded."
            )
            return
        draw_view.message = picker_message
        await self._finish(interaction, f"🤝 Draw picker opened for the **{title_data['name']}**.")

class SlayerMatchModal(discord.ui.Modal, title="Log Slayer Match"):
    """Asks for an optional deck link when logging from the context menu,
    since context menus can't take parameters. Leaving it blank is fine.
    """

    def __init__(self, pod_ids: List[int], source_url: Optional[str] = None,
                 spellbot_message_id: Optional[int] = None):
        # Timeout so an abandoned modal still frees the duplicate guard.
        super().__init__(timeout=SPELLBOT_MODAL_TIMEOUT)
        # Carried through so every exit can release the duplicate guard.
        self.spellbot_message_id = spellbot_message_id
        # True once the picker is on screen. After that the picker is in charge of
        # releasing the id, so this modal shouldn't.
        self._picker_sent = False
        # Pass the pod through so I don't have to read the SpellBot message again.
        self.pod_ids = pod_ids
        self.source_url = source_url

        self.decklist_input = discord.ui.TextInput(
            label="Moxfield deck URL (optional)",
            placeholder="https://www.moxfield.com/decks/...",
            required=False,
            max_length=200,
            style=discord.TextStyle.short,
        )
        self.add_item(self.decklist_input)

    async def on_submit(self, interaction: discord.Interaction):
        # An empty or blank field becomes None, same as /slayed with no decklist.
        # The or "" is there because value can be None.
        raw = (self.decklist_input.value or "").strip()
        decklist = raw or None

        # Check again since the modal could have been open a while. Discord rejects an empty select.
        if not db.get_titles(limit=TITLE_SELECT_LIMIT + 1):
            # No picker is coming, so release the id here.
            release_pending_spellbot_message(self.spellbot_message_id)
            try:
                await interaction.response.send_message(
                    "❌ No titles have been minted yet.", ephemeral=True
                )
            except discord.HTTPException:
                logger.exception("Claim modal empty-titles notice failed")
            return

        # The picker stays ephemeral since anyone could click it otherwise.
        # The public part is the verification message.
        view = ClaimSelectView(self.pod_ids, self.source_url, decklist=decklist,
                               spellbot_message_id=self.spellbot_message_id)
        try:
            await interaction.response.send_message(
                view.render_summary(interaction), view=view, ephemeral=True
            )
        except discord.HTTPException:
            # The picker never sent, so its timeout will never run. Release here.
            release_pending_spellbot_message(self.spellbot_message_id)
            logger.exception("Claim picker send from modal failed")
            return
        # The picker is in charge of the id now.
        self._picker_sent = True
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            # Only on_timeout needs view.message, so keep going.
            logger.exception("Claim picker message capture failed")

    async def on_timeout(self):
        """Runs if the modal was closed without submitting, or if on_submit crashed."""
        # Once the picker is showing it handles the release, not this modal.
        if not self._picker_sent:
            release_pending_spellbot_message(self.spellbot_message_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        # Log the real error and tell the user the match wasn't saved.
        logger.exception("Claim modal submission failed", exc_info=error)
        # Only release if the picker never showed up.
        if not self._picker_sent:
            release_pending_spellbot_message(self.spellbot_message_id)
        try:
            # The response might already be used.
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Something went wrong opening the match picker — nothing was recorded.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Something went wrong opening the match picker — nothing was recorded.",
                    ephemeral=True
                )
        except discord.HTTPException:
            logger.exception("Claim modal error notice failed")

@client.tree.context_menu(name="Log Slayer Match")
async def log_slayer_match(interaction: discord.Interaction, message: discord.Message):
    """Right click a SpellBot pod message to log it as a Slayer match.

    Nothing here writes to the database. It checks the pod, opens the decklist
    modal, and the picker hands it off to normal verification.
    All the checks run before send_modal because a modal has to be the first response.
    """
    if not is_spellbot_message(message):
        await interaction.response.send_message(
            "❌ That message doesn't look like a SpellBot match — no embed found.", ephemeral=True
        )
        return

    pod_ids = extract_pod_from_message(message)
    if len(pod_ids) != SPELLBOT_POD_SIZE:
        await interaction.response.send_message(
            f"❌ Found {len(pod_ids)} player(s) in that message; a Slayer match needs exactly {SPELLBOT_POD_SIZE}.",
            ephemeral=True
        )
        return

    # Only someone who was in the pod can log it.
    if interaction.user.id not in pod_ids:
        await interaction.response.send_message(
            "❌ You aren't one of the 4 players in that pod.", ephemeral=True
        )
        return

    # Discord rejects a select with no options.
    if not db.get_titles():
        await interaction.response.send_message(
            "❌ No titles have been minted yet.", ephemeral=True
        )
        return

    # Duplicate guard. Right clicking the same message twice would log the
    # same game twice.
    if message.id in pending_spellbot_messages:
        await interaction.response.send_message(DUPLICATE_LOG_WARNING, ephemeral=True)
        return

    # Add it before the await so two clicks at once can't both get through.
    pending_spellbot_messages.add(message.id)
    # If send_modal fails, release the id and re raise.
    try:
        await interaction.response.send_modal(
            SlayerMatchModal(pod_ids, message.jump_url, spellbot_message_id=message.id)
        )
    except Exception:
        release_pending_spellbot_message(message.id)
        raise

@client.tree.command(name="usurp", description="Log a pod win while the Title Champion is absent (4 verified wins = overthrow).")
async def usurp(interaction: discord.Interaction, title: str, decklist: Optional[str] = None):
    title_data = await get_title_or_error(interaction, title)
    if not title_data:
        return

    title_id = title_data['id']
    active_reign = db.get_active_reign_for_title(title_id)

    if not active_reign:
        await interaction.response.send_message(f"❌ The **{title}** is vacant — claim it directly with `/slayed`!", ephemeral=True)
        return

    champion_id = active_reign['discord_user_id']
    if interaction.user.id == champion_id:
        await interaction.response.send_message(f"❌ You ARE the champion of the **{title}** — log your defenses with `/slayed`.", ephemeral=True)
        return

    view = UsurpVerificationView(
        command_author_id=interaction.user.id,
        title_data=title_data,
        champion_id=champion_id,
        decklist=decklist
    )
    await interaction.response.send_message(
        f"🗡️ <@{interaction.user.id}> claims a pod win on the **{title}** while champion <@{champion_id}> was absent! "
        f"A pod member must verify this win.",
        view=view
    )
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        logger.exception("Verification message capture failed")

@usurp.autocomplete('title')
async def usurp_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

async def execute_reset_protocol():
    logger.info("Executing end-of-month reset protocol...")
    
    old_active_reigns = db.get_all_active_reigns_with_roles()
    active_reigns_to_announce = [r for r in old_active_reigns if r['discord_user_id']]
    
    iron_man = db.get_monthly_iron_man()
    
    if active_reigns_to_announce:
        announcement = "🎉 **END OF MONTH SLAYER RECAP** 🎉\n\nCongratulations to our final reigning Slayers of the month:\n"
        for reign in active_reigns_to_announce:
            display_str = get_member_display_string(reign['discord_user_id'])
            announcement += f"- **{reign['title_name']}**: {display_str}\n"
        announcement += "\nAll reigns have now been reset and archived to historical stats."
    else:
        announcement = "🎉 **END OF MONTH SLAYER RECAP** 🎉\n\nThere were no active Slayers at the end of this month. All records have been archived."
            
    if iron_man:
        display_str = get_member_display_string(iron_man['discord_user_id'])
        announcement += f"\n\n🛡️ **MONTHLY IRON MAN**: {display_str} with {iron_man['claims_this_month']} claims and {iron_man['defenses_this_month']} defenses!"

    for reign in old_active_reigns:
        user_id = reign['discord_user_id']
        role_id = reign['discord_role_id']
        if user_id and role_id:
            for guild in client.guilds:
                member = guild.get_member(user_id)
                role = guild.get_role(role_id)
                if member and role:
                    try:
                        await member.remove_roles(role, reason="End of month reset protocol")
                    except discord.HTTPException:
                        logger.exception(f"Failed to remove role {role.name} from {member.name}")
                        
    db.reset_all_active_reigns()
    
    new_active_reigns = db.get_all_active_reigns_with_roles()
    new_active_to_announce = [r for r in new_active_reigns if r['discord_user_id']]
    
    if new_active_to_announce:
        announcement += "\n\n👑 **THE LINEAL CHAMPIONS RETURN** 👑\nThe following titles have been reverted back to their Lineal Champions for the start of the new month:\n"
        for reign in new_active_to_announce:
            display_str = get_member_display_string(reign['discord_user_id'])
            announcement += f"- **{reign['title_name']}** has been reclaimed by {display_str}\n"
            
            role_id = reign['discord_role_id']
            user_id = reign['discord_user_id']
            if role_id and user_id:
                for guild in client.guilds:
                    member = guild.get_member(user_id)
                    role = guild.get_role(role_id)
                    if member and role:
                        try:
                            await member.add_roles(role, reason="Lineal Champion end of month reassignment")
                        except discord.HTTPException:
                            logger.exception(f"Failed to add role {role.name} to {member.name}")
    else:
         announcement += "\n\nA new month begins!"
         
    configs = db.get_announcement_channels()
    for config in configs:
        guild_id = config['guild_id']
        channel_id = config['announcement_channel_id']
        
        channel = client.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(announcement)
            except discord.HTTPException:
                logger.exception(f"Failed to send recap to channel {channel_id}")

    logger.info("End-of-month reset protocol completed.")

@tasks.loop(minutes=1)
async def monthly_recap():
    now = datetime.datetime.now(ZoneInfo("America/Chicago"))
    
    tomorrow = now + datetime.timedelta(days=1)
    is_last_day = tomorrow.month != now.month
    
    if is_last_day and now.hour == 23 and now.minute == 59:
        await execute_reset_protocol()

@monthly_recap.before_loop
async def before_monthly_recap():
    await client.wait_until_ready()

@tasks.loop(minutes=10)
async def life_upkeep_loop():
    try:
        upkeep_results = db.process_life_upkeeps()
        vacated_title_ids = upkeep_results.get("vacated", [])
        critical_title_ids = upkeep_results.get("critical", [])
        
        if not vacated_title_ids and not critical_title_ids:
            return
            
        configs = db.get_announcement_channels()
        
        for title_id in critical_title_ids:
            title_info = db.get_title_by_id(title_id)
            reign = db.get_active_reign_for_title(title_id)
            
            if reign and title_info:
                user_id = reign['discord_user_id']
                title_name = title_info['name']
                
                for config in configs:
                    guild_id = config['guild_id']
                    channel_id = config['announcement_channel_id']
                    guild = client.get_guild(guild_id)
                    if guild:
                        channel = guild.get_channel(channel_id)
                        if isinstance(channel, discord.TextChannel):
                            await channel.send(f"⚠️ <@{user_id}>, your life has dropped to 10! You have exactly 24 hours to defend the **{title_name}** before it decays!")
                            
        for title_id in vacated_title_ids:
            title_info = db.get_title_by_id(title_id)
            reign = db.get_active_reign_for_title(title_id)
            
            if reign and title_info:
                user_id = reign['discord_user_id']
                role_id = title_info['discord_role_id']
                title_name = title_info['name']
                
                db.vacate_title(title_id, is_bounty=True)
                
                for config in configs:
                    guild_id = config['guild_id']
                    channel_id = config['announcement_channel_id']
                    guild = client.get_guild(guild_id)
                    if guild:
                        if role_id:
                            member = guild.get_member(user_id)
                            role = guild.get_role(role_id)
                            if member and role:
                                try:
                                    await member.remove_roles(role, reason="Bled out due to inactivity")
                                except discord.Forbidden:
                                    channel = guild.get_channel(channel_id)
                                    if isinstance(channel, discord.TextChannel):
                                        await channel.send(f"Admin Alert: I tried to remove the {role.name} role from <@{user_id}>, but Discord blocked me! Please ensure the 'TTC_Slayer Bot' role is higher than the title roles and has 'Manage Roles' checked green in server settings")
                                except discord.HTTPException:
                                    logger.exception("Unexpected error removing role")
                                    
                        channel = guild.get_channel(channel_id)
                        if isinstance(channel, discord.TextChannel):
                            await channel.send(f"⚠️ A champion has bled out due to inactivity! The **{title_name}** is now Vacant and has a BOUNTY (+10 Starting Life)!")
                            
    except Exception:
        # Catch everything so the loop keeps running on the next tick.
        logger.exception("Error in life_upkeep_loop")

@life_upkeep_loop.before_loop
async def before_life_upkeep_loop():
    await client.wait_until_ready()

@client.tree.command(name="whoslayer", description="Shows all current Slayer title holders.")
async def whoslayer(interaction: discord.Interaction):
    holders = db.get_current_holders()
    
    description_lines = []
    for h in holders:
        if h['discord_user_id']:
            display_str = get_member_display_string(h['discord_user_id'], interaction)
            life = h['current_life']
            if life > 40:
                heart = "💙"
            elif life >= 21:
                heart = "💚"
            elif life >= 11:
                heart = "💛"
            else:
                heart = "❤️"
            streak_indicator = " 🔥" if h['defense_streak'] and h['defense_streak'] >= 3 else ""
            line = f"**{h['title_name']}**: {display_str} ({heart} {life}{streak_indicator} Life)"
            top_threat = db.get_top_contender(h['title_id'])
            if top_threat:
                threat_display = get_member_display_string(top_threat['discord_user_id'], interaction)
                line += f"\n⚠️ {threat_display} is plotting an overthrow ({top_threat['wins']}/{USURP_WINS_TO_OVERTHROW} Wins)"
            description_lines.append(line)
        elif h['sudden_death_contenders']:
            contender_ids = re.findall(r'\d+', h['sudden_death_contenders'])
            mentions = ", ".join(f"<@{cid}>" for cid in contender_ids)
            description_lines.append(f"**{h['title_name']}**: ⚔️ **CONTESTED!** Sudden Death Sprint between: {mentions}")
        else:
            if h['bounty_active']:
                description_lines.append(f"**{h['title_name']}**: 👑 **VACANT!** (Bounty Active: +10 Starting Life)")
            else:
                description_lines.append(f"**{h['title_name']}**: 👑 **VACANT!**")

    embed = discord.Embed(
        title="🏆 Current TC Slayers",
        description="\n\n".join(description_lines) if description_lines else "No titles found.",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="past_champions", description="Shows the End of Month champions from previous months.")
async def past_champions(interaction: discord.Interaction):
    past = db.get_past_champions()
    if not past:
        await interaction.response.send_message("No past champions found yet.", ephemeral=True)
        return
        
    embed = discord.Embed(
        title="📜 Past Champions",
        description="End of Month Slayer Champions",
        color=discord.Color.gold()
    )
    
    lines = []
    for champ in past:
        display_str = get_member_display_string(champ['discord_user_id'], interaction)
        lines.append(f"**{champ['title_name']}**: {display_str} (Defenses: {champ['total_defenses']})")
        
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=False)

@client.tree.command(name="slayerstats", description="View Slayer stats for a user.")
async def slayerstats(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    stats = db.get_user_stats(target.id)
    
    embed = discord.Embed(title=f"📊 Slayer Stats for {target.display_name}", color=discord.Color.blue())
    embed.add_field(name="Total Claims", value=str(stats['total_claims']), inline=True)
    embed.add_field(name="Max Defenses", value=str(stats['max_defenses']), inline=True)
    embed.add_field(name="Longest Reign", value=f"{stats['longest_reign_days']} days", inline=True)
    
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="hall_of_fame", description="View the all-time server records and historical performance.")
async def hall_of_fame(interaction: discord.Interaction):
    records = db.get_global_records()
    
    embed = discord.Embed(
        title="🏛️ TC Slayer Hall of Fame 🏛️",
        description="The greatest achievements in Slayer history.",
        color=discord.Color.gold()
    )
    
    if records["most_claims"]:
        user_id = records["most_claims"]["discord_user_id"]
        claims = records["most_claims"]["total_claims"]
        display_str = get_member_display_string(user_id, interaction)
        embed.add_field(name="Most Total Claims", value=f"{display_str} ({claims} claims)", inline=False)
        
    if records["most_defenses"]:
        user_id = records["most_defenses"]["discord_user_id"]
        defenses = records["most_defenses"]["max_defenses"]
        display_str = get_member_display_string(user_id, interaction)
        embed.add_field(name="Most Successful Defenses", value=f"{display_str} ({defenses} defenses in a single reign)", inline=False)
        
    if records["longest_reign"]:
        user_id = records["longest_reign"]["discord_user_id"]
        days = round(records["longest_reign"]["duration_days"], 1)
        display_str = get_member_display_string(user_id, interaction)
        embed.add_field(name="Longest All-Time Reign", value=f"{display_str} ({days} days)", inline=False)
        
    await interaction.response.send_message(embed=embed)

@client.tree.command(name="ironman_leaderboard", description="Shows the top 3 contenders in this month's Iron Man race.")
async def ironman_leaderboard(interaction: discord.Interaction):
    standings = db.get_ironman_standings(3)

    if not standings:
        await interaction.response.send_message("No reigns recorded this month yet.", ephemeral=False)
        return

    month_name = datetime.datetime.now(datetime.timezone.utc).strftime('%B %Y')
    medals = ["🥇", "🥈", "🥉"]

    lines = []
    for i, row in enumerate(standings):
        medal = medals[i] if i < len(medals) else "🎖️"
        display_str = get_member_display_string(row['discord_user_id'], interaction)
        lines.append(
            f"{medal} {display_str} — **{row['total_score']}** pts "
            f"({row['claims_this_month']} claims, {row['defenses_this_month']} defenses)"
        )

    embed = discord.Embed(
        title="🛡️ Iron Man Leaderboard",
        description=f"Standings for {month_name}\n\n" + "\n".join(lines),
        color=discord.Color.gold()
    )
    # Defenses are counted for reigns claimed this month.
    embed.set_footer(text="Counts reigns claimed this month")
    await interaction.response.send_message(embed=embed, ephemeral=False)

@client.tree.command(name="add_admin_role", description="Add a role that can bypass the self-verify block.")
@app_commands.default_permissions(administrator=True)
async def add_admin_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return
    db.add_admin_role(interaction.guild.id, role.id)
    await interaction.response.send_message(f"✅ Added {role.mention} to the admin roles list.", ephemeral=True)

@client.tree.command(name="remove_admin_role", description="Remove a role from the admin override list.")
@app_commands.default_permissions(administrator=True)
async def remove_admin_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return
    success = db.remove_admin_role(interaction.guild.id, role.id)
    if success:
        await interaction.response.send_message(f"✅ Removed {role.mention} from the admin roles list.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ {role.mention} is not in the admin roles list.", ephemeral=True)

@client.tree.command(name="set_original_holder", description="Set the lineal champion for a title.")
@app_commands.default_permissions(administrator=True)
async def set_original_holder(interaction: discord.Interaction, title: str, user: discord.Member):
    title_data = await get_title_or_error(interaction, title)
    if not title_data:
        return

    db.set_original_holder(title_data['id'], user.id)
    await interaction.response.send_message(f"✅ **{user.mention}** is now the Lineal Champion for **{title}**. They will automatically reclaim it at the end of the month.", ephemeral=True)

@set_original_holder.autocomplete('title')
async def set_original_holder_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="delete_title", description="Delete a Slayer title.")
@app_commands.default_permissions(administrator=True)
async def delete_title(interaction: discord.Interaction, title: str):
    await interaction.response.defer()

    title_data = await get_title_or_error(interaction, title, use_followup=True)
    if not title_data:
        return

    title_id = title_data['id']
    role_id = title_data['discord_role_id']

    db.delete_title(title_id)
    
    role_deleted = False
    if interaction.guild and role_id:
        role = interaction.guild.get_role(role_id)
        if role:
            try:
                await role.delete(reason=f"Title {title} deleted by {interaction.user}")
                role_deleted = True
            except (discord.Forbidden, discord.NotFound) as e:
                await interaction.followup.send(f"✅ Title **{title}** deleted from DB, but failed to delete Discord role: {e}", ephemeral=False)
                return
            except discord.HTTPException as e:
                await interaction.followup.send(f"✅ Title **{title}** deleted from DB, but failed to delete Discord role: {e}", ephemeral=False)
                return
    
    msg = f"✅ Successfully deleted title **{title}** from the DB."
    if role_deleted:
        msg = f"✅ Successfully deleted title **{title}** and its associated Discord role."
    
    await interaction.followup.send(msg, ephemeral=False)

@delete_title.autocomplete('title')
async def delete_title_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="edit_title", description="Edit an existing Slayer title.")
@app_commands.default_permissions(administrator=True)
async def edit_title(interaction: discord.Interaction, title: str, new_name: Optional[str] = None, new_role: Optional[discord.Role] = None):
    await interaction.response.defer()

    title_data = await get_title_or_error(interaction, title, use_followup=True)
    if not title_data:
        return

    title_id = title_data['id']
    old_role_id = title_data['discord_role_id']
    new_role_id = new_role.id if new_role else None
    
    db.edit_title(title_id, new_name=new_name, new_role_id=new_role_id)
    
    messages = []
    
    if new_name and interaction.guild and old_role_id:
        role = interaction.guild.get_role(old_role_id)
        if role:
            try:
                await role.edit(name=new_name, reason=f"Title renamed by {interaction.user}")
                messages.append(f"✅ Renamed Discord role to **{new_name}**.")
            except (discord.Forbidden, discord.NotFound) as e:
                messages.append(f"⚠️ Failed to rename Discord role: {e}")
            except discord.HTTPException as e:
                messages.append(f"⚠️ Failed to rename Discord role: {e}")
        else:
            messages.append("⚠️ Original Discord role not found in server, could not rename.")
            
    if new_role:
        messages.append(f"✅ Linked title to new role {new_role.mention}.")
    
    if new_name:
        messages.append(f"✅ Title name updated in DB to **{new_name}**.")
        
    if not messages:
        messages.append("✅ No changes were provided or necessary.")
        
    final_name = new_name if new_name else title
    messages.insert(0, f"**Changes to {final_name}:**")
    
    await interaction.followup.send("\n".join(messages), ephemeral=False)

@edit_title.autocomplete('title')
async def edit_title_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="force_cycle_reset", description="Manually execute the end-of-month reset protocol.")
@app_commands.default_permissions(administrator=True)
async def force_cycle_reset(interaction: discord.Interaction):
    await interaction.response.send_message("⚙️ Forcing the end-of-month cycle reset. Please wait...", ephemeral=True)
    await execute_reset_protocol()
    await interaction.followup.send("✅ Reset protocol complete.", ephemeral=True)

@client.tree.command(name="set_life", description="Set the life of an active champion.")
@app_commands.default_permissions(administrator=True)
async def set_life(interaction: discord.Interaction, title: str, life: int):
    title_data = await get_title_or_error(interaction, title)
    if not title_data:
        return
    active_reign = db.get_active_reign_for_title(title_data['id'])
    if not active_reign:
        await interaction.response.send_message(f"❌ **{title}** does not currently have an active reign.", ephemeral=True)
        return
    db.set_reign_stats(title_data['id'], new_life=life)
    await interaction.response.send_message(f"✅ Hand of God: {title} champion's life has been set to {life}.", ephemeral=False)

@set_life.autocomplete('title')
async def set_life_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="set_defenses", description="Set the defenses count of an active champion.")
@app_commands.default_permissions(administrator=True)
async def set_defenses(interaction: discord.Interaction, title: str, defenses: int):
    title_data = await get_title_or_error(interaction, title)
    if not title_data:
        return
    active_reign = db.get_active_reign_for_title(title_data['id'])
    if not active_reign:
        await interaction.response.send_message(f"❌ **{title}** does not currently have an active reign.", ephemeral=True)
        return
    db.set_reign_stats(title_data['id'], new_defenses=defenses)
    await interaction.response.send_message(f"✅ Hand of God: {title} defenses have been set to {defenses}.", ephemeral=False)

@set_defenses.autocomplete('title')
async def set_defenses_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="force_vacate", description="Forcibly vacate an active Slayer title.")
@app_commands.default_permissions(administrator=True)
async def force_vacate(interaction: discord.Interaction, title: str):
    await interaction.response.defer(ephemeral=True)
    title_data = await get_title_or_error(interaction, title, use_followup=True)
    if not title_data:
        return
    title_id = title_data['id']
    active_reign = db.get_active_reign_for_title(title_id)
    if not active_reign:
        await interaction.followup.send(f"❌ **{title}** does not currently have an active reign.", ephemeral=True)
        return
        
    user_id = active_reign['discord_user_id']
    role_id = title_data['discord_role_id']
    
    if interaction.guild and role_id:
        member = interaction.guild.get_member(user_id)
        role = interaction.guild.get_role(role_id)
        if member and role:
            try:
                await member.remove_roles(role, reason="Forcibly vacated by Admin")
            except discord.Forbidden:
                await interaction.followup.send(f"Admin Alert: I tried to remove the {role.name} role from <@{user_id}>, but Discord blocked me! Please ensure my role is higher in server settings.", ephemeral=True)
            except discord.HTTPException:
                logger.exception("Unexpected error removing role")
                
    db.vacate_title(title_id)
    
    configs = db.get_announcement_channels()
    msg = f"⚠️ ADMIN OVERRIDE: The {title} has been forcibly vacated!"
    announced = False
    if interaction.guild:
        for config in configs:
            if config['guild_id'] == interaction.guild.id:
                channel = interaction.guild.get_channel(config['announcement_channel_id'])
                if isinstance(channel, discord.TextChannel):
                    await channel.send(msg)
                    announced = True
    if not announced and isinstance(interaction.channel, discord.TextChannel):
        await interaction.channel.send(msg)
        
    await interaction.followup.send(f"✅ Successfully forcibly vacated **{title}**.", ephemeral=True)

@force_vacate.autocomplete('title')
async def force_vacate_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="undo_match", description="Admin: roll back a logged match by its Match ID.")
@app_commands.default_permissions(administrator=True)
async def undo_match(interaction: discord.Interaction, match_id: str):
    # Check permissions again since server owners can change the defaults.
    is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
    if not (is_admin or is_admin_override(interaction)):
        await interaction.response.send_message("❌ Only server administrators can reverse a match.", ephemeral=True)
        return

    # This can take longer than 3 seconds so I defer. Ephemeral so a typo
    # doesn't leave a public message, the success embed is sent separately.
    await interaction.response.defer(ephemeral=True)

    result = db.undo_match(match_id, interaction.user.id)
    status = result["status"]

    if status == "not_found":
        msg = f"❌ No match found with ID `{result['match_id']}`."
        recent = db.get_recent_matches(limit=5)
        if recent:
            lines = []
            for r in recent:
                title_row = db.get_title_by_id(r['title_id'])
                title_name = title_row['name'] if title_row else "unknown title"
                lines.append(f"`{r['match_id']}` ({r['match_type']} — {title_name})")
            msg += "\nRecent match IDs:\n" + "\n".join(lines)
        await interaction.followup.send(msg, ephemeral=True)
        return

    if status == "already_undone":
        await interaction.followup.send(
            f"❌ Match `{result['match_id']}` was already reversed by <@{result['undone_by']}> on {result['undone_at']}.",
            ephemeral=True
        )
        return

    if status == "stale":
        await interaction.followup.send(
            f"❌ Match `{result['match_id']}` is not the most recent match on this title. "
            f"Reverse `{result['blocking_match_id']}` ({result['blocking_match_type']}) first — "
            f"undoing out of order would discard the newer result.",
            ephemeral=True
        )
        return

    if status == "title_missing":
        await interaction.followup.send(
            f"❌ Match `{result['match_id']}` belongs to a title that no longer exists. "
            f"Reversing it would resurrect a reign for a deleted title, so nothing was changed.",
            ephemeral=True
        )
        return

    if status == "unlogged_change":
        await interaction.followup.send(
            f"❌ Match `{result['match_id']}` cannot be reversed: the title's history changed outside the "
            f"match log ({result['reason']}). Reversing it now would destroy records that were never logged.",
            ephemeral=True
        )
        return

    if status == "error":
        await interaction.followup.send(f"❌ The rollback failed and nothing was changed: {result['message']}", ephemeral=True)
        return

    # status is "ok"
    reverted_holder_id = result["reverted_holder_id"]
    restored_holder_id = result["restored_holder_id"]

    # If the undo changed who holds the title, move the role back too.
    # If that fails it has to show up in the embed.
    role_sync_failed = False
    if reverted_holder_id != restored_holder_id:
        title_row = db.get_title_by_id(result["title_id"])
        role_id = title_row['discord_role_id'] if title_row else None
        if interaction.guild and role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    if reverted_holder_id is not None:
                        reverted_member = interaction.guild.get_member(reverted_holder_id)
                        if reverted_member:
                            await reverted_member.remove_roles(role, reason=f"Match {result['match_id']} reversed by admin")
                        else:
                            # No members intent, so the member might not be cached.
                            role_sync_failed = True
                    if restored_holder_id is not None:
                        restored_member = interaction.guild.get_member(restored_holder_id)
                        if restored_member:
                            await restored_member.add_roles(role, reason=f"Match {result['match_id']} reversed by admin")
                        else:
                            role_sync_failed = True
                except discord.Forbidden:
                    # Forbidden is a subclass of HTTPException, so catch it first.
                    role_sync_failed = True
                    logger.exception("Role sync forbidden during /undo_match for %s", result['match_id'])
                except discord.HTTPException:
                    role_sync_failed = True
                    logger.exception("Role sync failed during /undo_match for %s", result['match_id'])
            else:
                role_sync_failed = True

    # Only list what actually changed.
    detail_lines = []
    if reverted_holder_id != restored_holder_id:
        if restored_holder_id is not None:
            detail_lines.append(f"👑 Title returned to <@{restored_holder_id}>")
        else:
            detail_lines.append("👑 Title returned to vacant")
        if reverted_holder_id is not None:
            detail_lines.append(f"↩️ <@{reverted_holder_id}> no longer holds the title")
    if result["restored_life"] is not None and result["restored_life"] != result["reverted_life"]:
        detail_lines.append(f"❤️ Life restored to {result['restored_life']}")
    if result["match_type"] == "defense" and result["restored_defenses"] is not None:
        detail_lines.append(f"🛡️ Defenses reverted to {result['restored_defenses']}")
    if result["sudden_death_cleared"]:
        detail_lines.append("⚔️ Sudden Death Sprint cancelled")
    if result["contenders_restored"]:
        detail_lines.append(f"🗡️ {result['contenders_restored']} contender streak(s) restored")
    # Check the key exists instead of crashing after the undo already ran.
    ratings_reverted = result["ratings_reverted"] if "ratings_reverted" in result else 0
    ratings_stale_for = result["ratings_stale_for"] if "ratings_stale_for" in result else 0
    if ratings_reverted:
        detail_lines.append(f"🎯 Ratings reverted: {ratings_reverted} player(s)")
    if not detail_lines:
        detail_lines.append("No state changes were required.")

    # Warnings about what the undo couldn't restore exactly.
    # Life decay doesn't get logged, so undoing an old match also gives that life back.
    if result["restored_life"] is not None:
        refunded_blocks = upkeep_blocks_elapsed(result["timestamp"])
        if refunded_blocks:
            detail_lines.append(
                f"⚠️ ~{refunded_blocks} upkeep block(s) of life decay were refunded by this rollback "
                f"— correct with `/set_life` if needed"
            )

    if role_sync_failed:
        detail_lines.append(
            "⚠️ Role sync failed — move my bot role higher (or check that I can see both members) "
            "and fix the title role manually"
        )

    embed = discord.Embed(title="⏪ Match Reversed", color=discord.Color.orange())
    embed.add_field(name="Match ID", value=f"`{result['match_id']}`", inline=True)
    embed.add_field(name="Type", value=result["match_type"].title(), inline=True)
    embed.add_field(name="Title", value=result["title_name"], inline=True)
    # This field can get long, so clamp it.
    embed.add_field(name="Reversal Details", value=clamp_field_value("\n".join(detail_lines)), inline=False)
    disclosure = (
        "This rollback only knows about logged matches. Anything that archives a reign without one — the "
        "monthly reset, an upkeep bleed-out, `/force_vacate`, `/grant_title` — is now detected and blocks the "
        "reversal outright, but state-only changes like `/set_life`, `/set_defenses`, `/toggle_bounty` and "
        "routine upkeep life decay leave no trace this command can see and are silently overwritten by it."
    )
    # If a player played another match since, their rating can't be restored exactly.
    if ratings_stale_for:
        disclosure += (
            f"\n\n🎯 {ratings_stale_for} player(s) could not have their rating restored exactly: they have "
            f"been rated in a later match that this undo does not touch. Their title, life and contender "
            f"state is exact — their Glicko number is not."
        )
    embed.add_field(
        name="⚠️ Disclosure",
        value=disclosure,
        inline=False
    )
    embed.set_footer(text=f"Reversed by {interaction.user.display_name} • originally logged {result['timestamp']} UTC")

    await interaction.followup.send(embed=embed)

@client.tree.command(name="toggle_bounty", description="Toggle a bounty on a vacant Slayer title.")
@app_commands.default_permissions(administrator=True)
async def toggle_bounty(interaction: discord.Interaction, title: str):
    title_data = await get_title_or_error(interaction, title)
    if not title_data:
        return
    title_id = title_data['id']
    active_reign = db.get_active_reign_for_title(title_id)
    if active_reign:
        await interaction.response.send_message("❌ Cannot put a bounty on an actively held title.", ephemeral=True)
        return
        
    current_status = bool(title_data['bounty_active'])
    if not current_status:
        db.set_bounty(title_id, True)
        msg = f"💰 ADMIN BOUNTY: A bounty has been placed on the vacant {title}! Next claim gets +10 Starting Life!"
        configs = db.get_announcement_channels()
        announced = False
        if interaction.guild:
            for config in configs:
                if config['guild_id'] == interaction.guild.id:
                    channel = interaction.guild.get_channel(config['announcement_channel_id'])
                    if isinstance(channel, discord.TextChannel):
                        await channel.send(msg)
                        announced = True
        if not announced and isinstance(interaction.channel, discord.TextChannel):
            await interaction.channel.send(msg)
        await interaction.response.send_message(f"✅ Placed bounty on **{title}**.", ephemeral=True)
    else:
        db.set_bounty(title_id, False)
        await interaction.response.send_message(f"✅ Removed bounty from {title}.", ephemeral=True)

@toggle_bounty.autocomplete('title')
async def toggle_bounty_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="bot_status", description="Check the system health and configuration status of the bot.")
@app_commands.default_permissions(administrator=True)
async def bot_status(interaction: discord.Interaction):
    ping_ms = round(client.latency * 1000)
    
    announcement_configured = "❌ Not Configured"
    if interaction.guild:
        configs = db.get_announcement_channels()
        if any(c['guild_id'] == interaction.guild.id for c in configs):
            announcement_configured = "✅ Configured"
            
    manage_roles_perm = "❌ False"
    if interaction.guild and interaction.guild.me:
        if interaction.guild.me.guild_permissions.manage_roles:
            manage_roles_perm = "✅ True"
            
    embed = discord.Embed(
        title="🤖 Bot Status & Health Check",
        color=discord.Color.green()
    )
    embed.add_field(name="Ping", value=f"{ping_ms}ms", inline=True)
    embed.add_field(name="Announcement Channel", value=announcement_configured, inline=True)
    embed.add_field(name="Manage Roles Permission", value=manage_roles_perm, inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="nemesis", description="Shows a user's most-defeated rival.")
async def nemesis(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    rival = db.get_top_rival(target.id)

    if not rival:
        await interaction.response.send_message(f"No rivalry history yet for {target.mention}.", ephemeral=True)
        return

    target_display = get_member_display_string(target.id, interaction)
    rival_display = get_member_display_string(rival['loser_id'], interaction)
    wins = rival['wins']

    embed = discord.Embed(
        title="⚔️ Nemesis",
        description=f"{target_display}'s greatest rival is {rival_display} — defeated {wins} time{'s' if wins != 1 else ''}!",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed, ephemeral=False)

@client.tree.command(name="title_history", description="Shows the recent holder history of a title.")
async def title_history(interaction: discord.Interaction, title: str):
    title_data = await get_title_or_error(interaction, title)
    if not title_data:
        return

    history = db.get_title_history(title_data['id'])

    embed = discord.Embed(
        title=f"📜 Title History: {title}",
        color=discord.Color.gold()
    )

    if not history:
        embed.description = "No history recorded for this title yet."
    else:
        lines = []
        for h in history:
            display_str = get_member_display_string(h['discord_user_id'], interaction)
            line = f"{display_str} — {h['total_defenses']} defenses"
            if h['decklist']:
                line += f" — Deck: {h['decklist']}"
            lines.append(line)
        embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed, ephemeral=False)

@title_history.autocomplete('title')
async def title_history_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return title_autocomplete_choices(current)

@client.tree.command(name="bounties", description="Lists all titles currently flagged with an active bounty.")
async def bounties(interaction: discord.Interaction):
    bounty_titles = db.get_bounty_titles()

    if not bounty_titles:
        await interaction.response.send_message("No active bounties right now.", ephemeral=True)
        return

    lines = [f"💰 **{t['name']}** — Next claim starts with {BOUNTY_STARTING_LIFE} life!" for t in bounty_titles]
    embed = discord.Embed(
        title="💰 Bounty Board",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=False)

@client.tree.command(name="profile", description="Show a player's Slayer rating, record, and commanders.")
async def profile(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user

    # Defer since there are several queries.
    await interaction.response.defer()

    rating = db.get_player_rating(target.id)
    rank_info = db.get_rating_rank(target.id)
    record = db.get_player_match_record(target.id)
    commanders = db.get_player_commanders(target.id, limit=5)
    titles = db.get_player_title_summary(target.id)

    embed = discord.Embed(title=f"⚔️ {target.display_name}", color=discord.Color.blurple())

    display_avatar = getattr(target, "display_avatar", None)
    if display_avatar is not None:
        embed.set_thumbnail(url=display_avatar.url)

    rating_value = f"**{rating['rating']:.0f}** ± {rating['rd']:.0f}"
    if rank_info['rank'] is not None:
        rating_value += f"\nRank {rank_info['rank']} of {rank_info['total']}"
    if rating['is_provisional']:
        rating_value += " *(provisional)*"
    embed.add_field(name="Rating", value=clamp_field_value(rating_value), inline=False)

    record_value = (
        f"{record['matches']} matches — {record['wins']}W / {record['draws']}D / {record['losses']}L\n"
        f"Win rate: {record['win_rate']}%"
    )
    embed.add_field(name="Record", value=clamp_field_value(record_value), inline=False)

    if titles['active_titles']:
        title_lines = [
            f"• {t['title_name']} — {t['defenses']} defense(s), {t['current_life']} life"
            for t in titles['active_titles']
        ]
    else:
        title_lines = ["None held"]
    title_lines.append(f"Lifetime defenses: {titles['lifetime_defenses']} • Titles held: {titles['titles_held']}")
    embed.add_field(name="Titles", value=clamp_field_value("\n".join(title_lines)), inline=False)

    if commanders:
        commander_lines = [
            f"• {c['commander_name']} ({format_color_identity(c['color_identity'])}) — {c['matches']} played, {c['win_rate']}% WR"
            for c in commanders
        ]
    else:
        commander_lines = ["No decks logged yet"]
    embed.add_field(name="Commanders", value=clamp_field_value("\n".join(commander_lines)), inline=False)

    try:
        await interaction.followup.send(embed=embed)
    except discord.HTTPException:
        logger.exception("Profile embed send failed for user %s", target.id)

@client.tree.command(name="server_meta", description="Server-wide cEDH meta breakdown from logged matches.")
async def server_meta(interaction: discord.Interaction):
    # Defer since there are several queries.
    await interaction.response.defer()

    meta = db.get_server_meta(limit=5)

    if meta['total_matches'] == 0:
        embed = discord.Embed(
            title="📈 Server Meta",
            description="No matches have been logged yet — play some games!",
            color=discord.Color.blurple()
        )
        try:
            await interaction.followup.send(embed=embed)
        except discord.HTTPException:
            logger.exception("Server meta empty-state embed send failed")
        return

    embed = discord.Embed(title="📈 Server Meta", color=discord.Color.blurple())

    top_commander_lines = [
        f"{i}. **{c['commander_name']}** ({format_color_identity(c['color_identity'])}) — {c['matches']} played, {c['win_rate']}% WR"
        for i, c in enumerate(meta['top_commanders'], start=1)
    ]
    if not top_commander_lines:
        top_commander_lines = ["No decklists logged yet"]
    embed.add_field(name="Top Commanders", value=clamp_field_value("\n".join(top_commander_lines)), inline=False)

    # Only show the top 8 so the field stays under 1024 characters.
    color_entries = meta['color_breakdown']
    shown = color_entries[:8]
    color_lines = [
        f"{format_color_identity(c['color_identity'])} — {c['matches']} ({c['share']}%)"
        for c in shown
    ]
    remaining = len(color_entries) - len(shown)
    if remaining > 0:
        color_lines.append(f"…and {remaining} more")
    if not color_lines:
        color_lines = ["No decklists logged yet"]
    embed.add_field(name="Color Identity Meta", value=clamp_field_value("\n".join(color_lines)), inline=False)

    totals_value = (
        f"Total matches: {meta['total_matches']}\n"
        # This counts decks with a known commander, not matches.
        f"Attributed decks: {meta['attributed_matches']}\n"
        f"Active players: {meta['active_players']}\n"
        f"Rated players: {meta['rated_players']}"
    )
    embed.add_field(name="Totals", value=clamp_field_value(totals_value), inline=False)

    embed.set_footer(text="Commander data only exists for matches where a Moxfield decklist was supplied.")

    try:
        await interaction.followup.send(embed=embed)
    except discord.HTTPException:
        logger.exception("Server meta embed send failed")

@client.tree.command(name="leaderboard", description="Top Glicko-2 rated players on the server.")
async def leaderboard(interaction: discord.Interaction):
    # Defer first in case the query is slow.
    await interaction.response.defer()

    entries = db.get_rating_leaderboard(limit=10)

    if not entries:
        embed = discord.Embed(
            title="🏆 Slayer Leaderboard",
            description="No rated matches have been logged yet — ratings appear once matches are verified.",
            color=discord.Color.gold()
        )
        try:
            await interaction.followup.send(embed=embed)
        except discord.HTTPException:
            logger.exception("Leaderboard empty-state embed send failed")
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, entry in enumerate(entries, start=1):
        medal = medals[i] if i in medals else f"{i}."
        display = get_member_display_string(entry['user_id'], interaction)
        provisional_marker = "*" if entry['rd'] >= PROVISIONAL_RD else ""
        lines.append(f"{medal} {display} — {entry['rating']:.0f} ± {entry['rd']:.0f}{provisional_marker}")

    embed = discord.Embed(title="🏆 Slayer Leaderboard", color=discord.Color.gold())
    embed.add_field(name="Top 10", value=clamp_field_value("\n".join(lines)), inline=False)
    embed.set_footer(
        text="* = provisional (still settling). Ranked by rating minus 2x RD, the bottom of "
             "the confidence interval, so a lucky new player can't outrank a proven regular."
    )

    try:
        await interaction.followup.send(embed=embed)
    except discord.HTTPException:
        logger.exception("Leaderboard embed send failed")

@client.tree.command(name="slayer_rules", description="How the Slayer system works: life, usurpations, ratings, and the SpellBot context menu.")
async def slayer_rules(interaction: discord.Interaction):
    """Static rules card. All the numbers come from the constants so it stays
    correct if I change the balance.
    """
    embed = discord.Embed(
        title="TC Slayer Bot - Rules & Economy",
        description=(
            "Every result below is peer-verified: the player who logs a match never "
            "confirms it themselves, except under an admin override, which is tagged "
            "**(Admin Override)** in the announcement. Nothing is written until somebody "
            "else clicks the check - or an admin does, in that one case."
        ),
        color=discord.Color.gold()
    )

    embed.add_field(
        name="❤️ **Life Economy**",
        value=clamp_field_value(
            f"• A new champion is crowned with **{DEFAULT_STARTING_LIFE} life**.\n"
            f"• Claiming a title that carries an active **bounty** starts you at "
            f"**{BOUNTY_STARTING_LIFE} life** instead.\n"
            f"• A successful defense lifelinks the champion back up to "
            f"**{DEFAULT_STARTING_LIFE} life**, plus a Momentum Bonus on top every third "
            f"consecutive defense.\n"
            f"• A verified **draw** costs the champion **{DRAW_DAMAGE} life**. Reach 0 and the "
            f"title is vacated into a Sudden Death Sprint between the players who drew into it.\n"
            f"• `/slayed` has no way to know who else was at the table, so a `/slayed` draw "
            f"**always** applies this damage to the sitting champion. The one carve-out is a "
            f"draw logged via the **context menu** in a pod the champion genuinely wasn't in - "
            f"see the context-menu section below."
        ),
        inline=False
    )

    embed.add_field(
        name="⚡ **Usurpations**",
        value=clamp_field_value(
            f"What makes a pod win a *usurpation* is the champion **not being at the table** - "
            f"beat the champion in person and it is an ordinary claim instead.\n"
            f"• Each verified pod win while the champion is absent adds 1 to your contender "
            f"streak on that title.\n"
            f"• **{USURP_WARNING_WINS} wins** puts the champion on notice.\n"
            f"• **{USURP_WINS_TO_OVERTHROW} wins** overthrows them on the spot - and the "
            f"usurper is crowned with **{BOUNTY_STARTING_LIFE} life**, not "
            f"{DEFAULT_STARTING_LIFE}.\n"
            f"• Any verified claim or defense on the title **wipes its entire contender "
            f"board** back to zero, not just a defense.\n"
            f"• A contender win counts the same whether it was logged with `/usurp` or the "
            f"context menu. Whether it also moves anyone's **rating** does not - see below."
        ),
        inline=False
    )

    embed.add_field(
        name="📈 **Glicko-2 Rating**",
        value=clamp_field_value(
            "Ratings need to know **who was actually at the table**. Nothing is ever invented "
            "for an opponent the bot cannot name, so how a match was logged decides how much of "
            "it gets rated.\n"
            "• Logged from the **context menu** - or a draw whose pod you pick in the draw "
            "picker - all four players are rated against each other.\n"
            "• A bare `/slayed` claim knows only the outgoing champion, so it rates just "
            "those two - and nothing at all on a vacant title. A bare `/slayed` defense and a "
            "bare `/usurp` know no opponents at all and rate **nobody**.\n"
            "• Where a match was rated, the announcement shows each player's exact rating "
            "change.\n"
            "• A new player's rating starts **provisional** - deliberately not trusted until "
            "enough games have pinned it down - and firms up as they play.\n"
            "• `/leaderboard`, `/profile` and `/server_meta` all read this rating."
        ),
        inline=False
    )

    embed.add_field(
        name="🖱️ **Logging with the SpellBot context menu**",
        value=clamp_field_value(
            f"Right-click (or long-press on mobile) a SpellBot match message, then **Apps** > "
            f"**Log Slayer Match**.\n"
            f"• It reads all {SPELLBOT_POD_SIZE} pod members straight off the message - no "
            f"retyping.\n"
            f"• It offers an optional **Moxfield decklist** field, so a pod logged this way "
            f"still records your commander.\n"
            f"• It **auto-detects** the result: a defense if the champion is the one logging "
            f"it, otherwise a claim or a usurpation depending on whether the champion was "
            f"actually in that pod.\n"
            f"• A draw logged **this way** in a pod the champion was **not** in is recorded "
            f"and rated as a pod draw with no title effect - no {DRAW_DAMAGE} damage, no "
            f"Sudden Death. This carve-out only exists for context-menu logging; a `/slayed` "
            f"draw always costs the champion life (see Life Economy above)."
        ),
        inline=False
    )

    embed.set_footer(text="Logging from a SpellBot message requires you to be one of the four players in that pod.")

    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="help", description="Displays a user guide for the TC Slayer bot.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="TC Slayer Bot - User Guide",
        description="Here are the commands you can use to interact with the Slayer system.",
        color=discord.Color.gold()
    )
    
    embed.add_field(
        name="🎮 **/slayed**",
        value="The core command. Initiates a steal (if you don't hold the title) or a defense (if you do). Requires another player to click ✅ to verify.",
        inline=False
    )

    embed.add_field(
        name="🎮 **/usurp**",
        value="Log a pod win while the Title Champion is absent. Takes an optional `decklist` (Moxfield URL) so the win records your commander. Requires peer verification. 3 verified wins warns the champion; 4 wins overthrows them! A champion defense via /slayed wipes all contender streaks.",
        inline=False
    )

    embed.add_field(
        name="🎮 **/whoslayer**",
        value="Displays a live leaderboard of active titles and current holders.",
        inline=False
    )
    
    embed.add_field(
        name="🎮 **/slayerstats**",
        value="Displays historical stats (total claims, max defenses, longest reign) for a user.",
        inline=False
    )
    
    embed.add_field(
        name="🎮 **/hall_of_fame**",
        value="View the all-time server records and historical performance.",
        inline=False
    )

    embed.add_field(
        name="🎮 **/ironman_leaderboard**",
        value="Shows the top 3 contenders in this month's Iron Man race (claims + defenses).",
        inline=False
    )

    embed.add_field(
        name="🎮 **/nemesis**",
        value="Shows a user's most-defeated rival (the opponent they've beaten the most).",
        inline=False
    )

    embed.add_field(
        name="🎮 **/title_history**",
        value="Shows the last 10 holders of a title, with defenses and any recorded decklists.",
        inline=False
    )

    embed.add_field(
        name="🎮 **/bounties**",
        value="Lists all vacant titles currently flagged with an active bounty.",
        inline=False
    )

    embed.add_field(
        name="🎮 **/profile**",
        value="Shows your (or another player's) Slayer rating, match record, active titles, and most-played commanders.",
        inline=False
    )

    embed.add_field(
        name="🎮 **/server_meta**",
        value="Server-wide cEDH meta breakdown: top commanders, color identity spread, and match totals from logged matches.",
        inline=False
    )

    embed.add_field(
        name="🎮 **/leaderboard**",
        value="Shows the Top 10 Glicko-2 rated players on the server, ranked by rating minus 2x RD.",
        inline=False
    )

    embed.add_field(
        name="🎮 **Log Slayer Match** (right-click a message)",
        value="Right-click a SpellBot pod announcement, then Apps → Log Slayer Match, to log its result as a Slayer match without retyping the pod. Reads all 4 players automatically, offers an optional Moxfield decklist, and auto-detects whether the result is a claim, a defense or a usurpation.",
        inline=False
    )

    embed.add_field(
        name="🎮 **/slayer_rules**",
        value="The full rules card: life totals and draw damage, how usurpations and overthrows work, how Glicko-2 ratings are earned, and how to log a match from a SpellBot message.",
        inline=False
    )

    embed.add_field(
        name="⚙️ Administrator Commands",
        value="`/mint_title`, `/grant_title`, `/set_original_holder`, `/set_announcement_channel`, `/force_cycle_reset`, `/add_admin_role`, `/remove_admin_role`, `/delete_title`, `/edit_title`, `/set_life`, `/set_defenses`, `/force_vacate`, `/toggle_bounty`, `/undo_match`, `/bot_status`",
        inline=False
    )
    
    embed.set_footer(text="⚠️ Titles will decay if not defended within 96 hours!")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

if __name__ == '__main__':
    if not TOKEN:
        logger.error("No DISCORD_TOKEN found in environment variables. Please set it in your .env file.")
    else:
        client.run(TOKEN)
