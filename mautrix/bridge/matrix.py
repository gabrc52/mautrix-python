# Copyright (c) 2021 Tulir Asokan
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from __future__ import annotations

import asyncio
import logging
import sys
import time

from mautrix import __optional_imports__
from mautrix.appservice import AppService
from mautrix.appservice.api.intent import DOUBLE_PUPPET_SOURCE_KEY
from mautrix.errors import (
    DecryptionError,
    IntentError,
    MatrixError,
    MExclusive,
    MForbidden,
    MUnknownToken,
    SessionNotFound,
)
from mautrix.types import (
    BaseRoomEvent,
    EncryptedEvent,
    Event,
    EventID,
    EventType,
    MediaRepoConfig,
    Membership,
    MemberStateEventContent,
    MessageEvent,
    MessageEventContent,
    MessageType,
    PresenceEvent,
    ReactionEvent,
    ReceiptEvent,
    ReceiptType,
    RedactionEvent,
    RoomID,
    SingleReceiptEventContent,
    StateEvent,
    StateUnsigned,
    TextMessageEventContent,
    TypingEvent,
    UserID,
)
from mautrix.util import markdown
from mautrix.util.logging import TraceLogger
from mautrix.util.message_send_checkpoint import (
    CHECKPOINT_TYPES,
    MessageSendCheckpoint,
    MessageSendCheckpointReportedBy,
    MessageSendCheckpointStatus,
    MessageSendCheckpointStep,
)
from mautrix.util.opt_prometheus import Histogram

from .. import bridge as br
from . import commands as cmd

encryption_import_error = None
media_encrypt_import_error = None

try:
    from .e2ee import EncryptionManager
except ImportError as e:
    if __optional_imports__:
        raise
    encryption_import_error = e
    EncryptionManager = None

try:
    from mautrix.crypto.attachments import encrypt_attachment
except ImportError as e:
    if __optional_imports__:
        raise
    media_encrypt_import_error = e
    encrypt_attachment = None

EVENT_TIME = Histogram(
    "bridge_matrix_event", "Time spent processing Matrix events", ["event_type"]
)


class BaseMatrixHandler:
    log: TraceLogger = logging.getLogger("mau.mx")
    az: AppService
    commands: cmd.CommandProcessor
    config: config.BaseBridgeConfig
    bridge: br.Bridge
    e2ee: EncryptionManager | None
    media_config: MediaRepoConfig

    user_id_prefix: str
    user_id_suffix: str

    def __init__(
        self,
        command_processor: cmd.CommandProcessor | None = None,
        bridge: br.Bridge | None = None,
    ) -> None:
        self.az = bridge.az
        self.config = bridge.config
        self.bridge = bridge
        self.commands = command_processor or cmd.CommandProcessor(bridge=bridge)
        self.media_config = MediaRepoConfig(upload_size=50 * 1024 * 1024)
        self.az.matrix_event_handler(self.int_handle_event)

        self.e2ee = None
        if self.config["bridge.encryption.allow"]:
            if not EncryptionManager:
                self.log.fatal(
                    "Encryption enabled in config, but dependencies not installed.",
                    exc_info=encryption_import_error,
                )
                sys.exit(31)
            if not encrypt_attachment:
                self.log.fatal(
                    "Encryption enabled in config, but media encryption dependencies "
                    "not installed.",
                    exc_info=media_encrypt_import_error,
                )
                sys.exit(31)
            self.e2ee = EncryptionManager(
                bridge=bridge,
                user_id_prefix=self.user_id_prefix,
                user_id_suffix=self.user_id_suffix,
                homeserver_address=self.config["homeserver.address"],
                db_url=self.config["appservice.database"],
                key_sharing_config=self.config["bridge.encryption.key_sharing"],
            )

        self.management_room_text = self.config.get(
            "bridge.management_room_text",
            {
                "welcome": "Hello, I'm a bridge bot.",
                "welcome_connected": "Use `help` for help.",
                "welcome_unconnected": "Use `help` for help on how to log in.",
            },
        )
        self.management_room_multiple_messages = self.config.get(
            "bridge.management_room_multiple_messages",
            False,
        )

    async def wait_for_connection(self) -> None:
        self.log.info("Ensuring connectivity to homeserver")
        errors = 0
        tried_to_register = False
        while True:
            try:
                await self.az.intent.whoami()
                break
            except (MUnknownToken, MExclusive):
                # These are probably not going to resolve themselves by waiting
                raise
            except MForbidden:
                if not tried_to_register:
                    self.log.debug(
                        "Whoami endpoint returned M_FORBIDDEN, "
                        "trying to register bridge bot before retrying..."
                    )
                    await self.az.intent.ensure_registered()
                    tried_to_register = True
                else:
                    raise
            except Exception:
                errors += 1
                if errors <= 6:
                    self.log.exception("Connection to homeserver failed, retrying in 10 seconds")
                    await asyncio.sleep(10)
                else:
                    raise
        try:
            self.media_config = await self.az.intent.get_media_repo_config()
        except Exception:
            self.log.warning("Failed to fetch media repo config", exc_info=True)

    async def init_as_bot(self) -> None:
        self.log.debug("Initializing appservice bot")
        displayname = self.config["appservice.bot_displayname"]
        if displayname:
            try:
                await self.az.intent.set_displayname(
                    displayname if displayname != "remove" else ""
                )
            except Exception:
                self.log.exception("Failed to set bot displayname")

        avatar = self.config["appservice.bot_avatar"]
        if avatar:
            try:
                await self.az.intent.set_avatar_url(avatar if avatar != "remove" else "")
            except Exception:
                self.log.exception("Failed to set bot avatar")

    async def init_encryption(self) -> None:
        if self.e2ee:
            await self.e2ee.start()

    async def allow_message(self, user: br.BaseUser) -> bool:
        return user.is_whitelisted or (
            self.config["bridge.relay.enabled"] and user.relay_whitelisted
        )

    @staticmethod
    async def allow_command(user: br.BaseUser) -> bool:
        return user.is_whitelisted

    @staticmethod
    async def allow_bridging_message(user: br.BaseUser, portal: br.BasePortal) -> bool:
        return await user.is_logged_in() or (user.relay_whitelisted and portal.has_relay)

    async def handle_leave(self, room_id: RoomID, user_id: UserID, event_id: EventID) -> None:
        pass

    async def handle_kick(
        self, room_id: RoomID, user_id: UserID, kicked_by: UserID, reason: str, event_id: EventID
    ) -> None:
        pass

    async def handle_ban(
        self, room_id: RoomID, user_id: UserID, banned_by: UserID, reason: str, event_id: EventID
    ) -> None:
        pass

    async def handle_unban(
        self, room_id: RoomID, user_id: UserID, unbanned_by: UserID, reason: str, event_id: EventID
    ) -> None:
        pass

    async def handle_join(self, room_id: RoomID, user_id: UserID, event_id: EventID) -> None:
        pass

    async def handle_member_info_change(
        self,
        room_id: RoomID,
        user_id: UserID,
        content: MemberStateEventContent,
        prev_content: MemberStateEventContent,
        event_id: EventID,
    ) -> None:
        pass

    async def handle_puppet_invite(
        self, room_id: RoomID, puppet: br.BasePuppet, invited_by: br.BaseUser, event_id: EventID
    ) -> None:
        pass

    async def handle_invite(
        self, room_id: RoomID, user_id: UserID, inviter: br.BaseUser, event_id: EventID
    ) -> None:
        pass

    async def handle_reject(
        self, room_id: RoomID, user_id: UserID, reason: str, event_id: EventID
    ) -> None:
        pass

    async def handle_disinvite(
        self,
        room_id: RoomID,
        user_id: UserID,
        disinvited_by: UserID,
        reason: str,
        event_id: EventID,
    ) -> None:
        pass

    async def handle_event(self, evt: Event) -> None:
        """
        Called by :meth:`int_handle_event` for message events other than m.room.message.

        **N.B.** You may need to add the event class to :attr:`allowed_event_classes`
        or override :meth:`allow_matrix_event` for it to reach here.
        """

    async def handle_state_event(self, evt: StateEvent) -> None:
        """
        Called by :meth:`int_handle_event` for state events other than m.room.membership.

        **N.B.** You may need to add the event class to :attr:`allowed_event_classes`
        or override :meth:`allow_matrix_event` for it to reach here.
        """

    async def handle_ephemeral_event(
        self, evt: ReceiptEvent | PresenceEvent | TypingEvent
    ) -> None:
        if evt.type == EventType.RECEIPT:
            await self.handle_receipt(evt)

    async def send_permission_error(self, room_id: RoomID) -> None:
        await self.az.intent.send_notice(
            room_id,
            text=(
                "You are not whitelisted to use this bridge.\n\n"
                "If you are the owner of this bridge, see the bridge.permissions "
                "section in your config file."
            ),
            html=(
                "<p>You are not whitelisted to use this bridge.</p>"
                "<p>If you are the owner of this bridge, see the "
                "<code>bridge.permissions</code> section in your config file.</p>"
            ),
        )

    async def accept_bot_invite(self, room_id: RoomID, inviter: br.BaseUser) -> None:
        tries = 0
        while tries < 5:
            try:
                await self.az.intent.join_room(room_id)
                break
            except (IntentError, MatrixError):
                tries += 1
                wait_for_seconds = (tries + 1) * 10
                if tries < 5:
                    self.log.exception(
                        f"Failed to join room {room_id} with bridge bot, "
                        f"retrying in {wait_for_seconds} seconds..."
                    )
                    await asyncio.sleep(wait_for_seconds)
                else:
                    self.log.exception(f"Failed to join room {room_id}, giving up.")
                    return

        if not await self.allow_command(inviter):
            await self.send_permission_error(room_id)
            await self.az.intent.leave_room(room_id)
            return

        await self.send_welcome_message(room_id, inviter)

    async def send_welcome_message(self, room_id: RoomID, inviter: br.BaseUser) -> None:
        has_two_members, bridge_bot_in_room = await self._is_direct_chat(room_id)
        is_management = has_two_members and bridge_bot_in_room

        welcome_messages = [self.management_room_text.get("welcome")]

        if is_management:
            if await inviter.is_logged_in():
                welcome_messages.append(self.management_room_text.get("welcome_connected"))
            else:
                welcome_messages.append(self.management_room_text.get("welcome_unconnected"))

            additional_help = self.management_room_text.get("additional_help")
            if additional_help:
                welcome_messages.append(additional_help)
        else:
            cmd_prefix = self.commands.command_prefix
            welcome_messages.append(f"Use `{cmd_prefix} help` for help.")

        if self.management_room_multiple_messages:
            for m in welcome_messages:
                await self.az.intent.send_notice(room_id, text=m, html=markdown.render(m))
        else:
            combined = "\n".join(welcome_messages)
            combined_html = "".join(map(markdown.render, welcome_messages))
            await self.az.intent.send_notice(room_id, text=combined, html=combined_html)

    async def int_handle_invite(
        self, room_id: RoomID, user_id: UserID, invited_by: UserID, event_id: EventID
    ) -> None:
        self.log.debug(f"{invited_by} invited {user_id} to {room_id}")
        inviter = await self.bridge.get_user(invited_by)
        if inviter is None:
            self.log.exception(f"Failed to find user with Matrix ID {invited_by}")
            return
        elif user_id == self.az.bot_mxid:
            await self.accept_bot_invite(room_id, inviter)
            return
        elif not await self.allow_command(inviter):
            return

        puppet = await self.bridge.get_puppet(user_id)
        if puppet:
            await self.handle_puppet_invite(room_id, puppet, inviter, event_id)
            return

        await self.handle_invite(room_id, user_id, inviter, event_id)

    def is_command(self, message: MessageEventContent) -> tuple[bool, str]:
        text = message.body
        prefix = self.config["bridge.command_prefix"]
        is_command = text.startswith(prefix)
        if is_command:
            text = text[len(prefix) + 1 :].lstrip()
        return is_command, text

    async def handle_message(
        self, room_id: RoomID, user_id: UserID, message: MessageEventContent, event_id: EventID
    ) -> None:
        async def bail(error_text: str, step=MessageSendCheckpointStep.REMOTE) -> None:
            self.log.debug(error_text)
            await MessageSendCheckpoint(
                event_id=event_id,
                room_id=room_id,
                step=step,
                timestamp=int(time.time() * 1000),
                status=MessageSendCheckpointStatus.PERM_FAILURE,
                reported_by=MessageSendCheckpointReportedBy.BRIDGE,
                event_type=EventType.ROOM_MESSAGE,
                message_type=message.msgtype,
                info=error_text,
            ).send(
                self.log,
                self.bridge.config["homeserver.message_send_checkpoint_endpoint"],
                self.az.as_token,
            )

        sender = await self.bridge.get_user(user_id)
        if not sender or not await self.allow_message(sender):
            await bail(
                f"Ignoring message {event_id} from {user_id} to {room_id}:"
                " user is not whitelisted."
            )
            return
        self.log.debug(f"Received Matrix event {event_id} from {sender.mxid} in {room_id}")
        self.log.trace("Event %s content: %s", event_id, message)

        if isinstance(message, TextMessageEventContent):
            message.trim_reply_fallback()

        is_command, text = self.is_command(message)
        portal = await self.bridge.get_portal(room_id)
        if not is_command and portal:
            if await self.allow_bridging_message(sender, portal):
                await portal.handle_matrix_message(sender, message, event_id)
            else:
                await bail(
                    f"Ignoring event {event_id} from {sender.mxid}:"
                    " not allowed to send to portal"
                )
            return

        if message.msgtype != MessageType.TEXT:
            await bail(f"Ignoring event {event_id}: not a portal room and not a m.text message")
            return
        elif not await self.allow_command(sender):
            await bail(
                f"Ignoring command {event_id} from {sender.mxid}: not allowed to perform command",
                step=MessageSendCheckpointStep.COMMAND,
            )
            return

        has_two_members, bridge_bot_in_room = await self._is_direct_chat(room_id)
        is_management = has_two_members and bridge_bot_in_room

        if is_command or is_management:
            try:
                command, arguments = text.split(" ", 1)
                args = arguments.split(" ")
            except ValueError:
                # Not enough values to unpack, i.e. no arguments
                command = text
                args = []

            try:
                await self.commands.handle(
                    room_id,
                    event_id,
                    sender,
                    command,
                    args,
                    message,
                    portal,
                    is_management,
                    bridge_bot_in_room,
                )
            except Exception as e:
                await bail(repr(e), step=MessageSendCheckpointStep.COMMAND)
            else:
                await MessageSendCheckpoint(
                    event_id=event_id,
                    room_id=room_id,
                    step=MessageSendCheckpointStep.COMMAND,
                    timestamp=int(time.time() * 1000),
                    status=MessageSendCheckpointStatus.SUCCESS,
                    reported_by=MessageSendCheckpointReportedBy.BRIDGE,
                    event_type=EventType.ROOM_MESSAGE,
                    message_type=message.msgtype,
                ).send(
                    self.log,
                    self.bridge.config["homeserver.message_send_checkpoint_endpoint"],
                    self.az.as_token,
                )
        else:
            await bail(
                f"Ignoring event {event_id} from {sender.mxid}:"
                " not a command and not a portal room"
            )

    async def _is_direct_chat(self, room_id: RoomID) -> tuple[bool, bool]:
        try:
            members = await self.az.intent.get_room_members(room_id)
            return len(members) == 2, self.az.bot_mxid in members
        except MatrixError:
            return False, False

    async def handle_receipt(self, evt: ReceiptEvent) -> None:
        for event_id, receipts in evt.content.items():
            for user_id, data in receipts[ReceiptType.READ].items():
                user = await self.bridge.get_user(user_id, create=False)
                if not user or not await user.is_logged_in():
                    continue

                portal = await self.bridge.get_portal(evt.room_id)
                if not portal:
                    continue

                await self.handle_read_receipt(user, portal, event_id, data)

    async def handle_read_receipt(
        self,
        user: br.BaseUser,
        portal: br.BasePortal,
        event_id: EventID,
        data: SingleReceiptEventContent,
    ) -> None:
        pass

    async def try_handle_sync_event(self, evt: Event) -> None:
        try:
            if isinstance(evt, (ReceiptEvent, PresenceEvent, TypingEvent)):
                await self.handle_ephemeral_event(evt)
            else:
                self.log.trace("Unknown event type received from sync: %s", evt)
        except Exception:
            self.log.exception("Error handling manually received Matrix event")

    async def send_encryption_error_notice(
        self, evt: EncryptedEvent, error: DecryptionError
    ) -> None:
        await self.az.intent.send_notice(
            evt.room_id, f"\u26a0 Your message was not bridged: {error}"
        )

    async def handle_encrypted(self, evt: EncryptedEvent) -> None:
        if not self.e2ee:
            self.send_decrypted_checkpoint(evt, "Encryption unsupported", True)
            await self.handle_encrypted_unsupported(evt)
            return
        try:
            decrypted = await self.e2ee.decrypt(evt, wait_session_timeout=5)
        except SessionNotFound as e:
            self.send_decrypted_checkpoint(evt, e, False)
            await self._handle_encrypted_wait(evt, e, wait=15)
        except DecryptionError as e:
            self.log.warning(f"Failed to decrypt {evt.event_id}: {e}")
            self.log.trace("%s decryption traceback:", evt.event_id, exc_info=True)
            self.send_decrypted_checkpoint(evt, e, True)
            await self.send_encryption_error_notice(evt, e)
        else:
            self.send_decrypted_checkpoint(decrypted)
            await self.int_handle_event(decrypted, send_bridge_checkpoint=False)

    async def handle_encrypted_unsupported(self, evt: EncryptedEvent) -> None:
        self.log.debug(
            "Got encrypted message %s from %s, but encryption is not enabled",
            evt.event_id,
            evt.sender,
        )
        await self.az.intent.send_notice(
            evt.room_id, "🔒️ This bridge has not been configured to support encryption"
        )

    async def _handle_encrypted_wait(
        self, evt: EncryptedEvent, err: SessionNotFound, wait: int
    ) -> None:
        self.log.debug(
            f"Couldn't find session {err.session_id} trying to decrypt {evt.event_id},"
            " waiting even longer"
        )
        msg = (
            "\u26a0 Your message was not bridged: the bridge hasn't received the decryption "
            f"keys. The bridge will retry for {wait} seconds. If this error keeps happening, "
            "try restarting your client."
        )
        asyncio.create_task(
            self.e2ee.crypto.request_room_key(
                evt.room_id,
                evt.content.sender_key,
                evt.content.session_id,
                from_devices={evt.sender: [evt.content.device_id]},
            )
        )
        try:
            event_id = await self.az.intent.send_notice(evt.room_id, msg)
        except IntentError:
            self.log.debug("IntentError while sending encryption error", exc_info=True)
            self.log.error(
                "Got IntentError while trying to send encryption error message. "
                "This likely means the bridge bot is not in the room, which can "
                "happen if you force-enable e2ee on the homeserver without enabling "
                "it by default on the bridge (bridge -> encryption -> default)."
            )
            return
        got_keys = await self.e2ee.crypto.wait_for_session(
            evt.room_id, err.sender_key, err.session_id, timeout=wait
        )
        if got_keys:
            self.log.debug(
                f"Got session {err.session_id} after waiting more, "
                f"trying to decrypt {evt.event_id} again"
            )
            try:
                decrypted = await self.e2ee.decrypt(evt, wait_session_timeout=0)
            except DecryptionError as e:
                self.send_decrypted_checkpoint(evt, e, True, retry_num=1)
                self.log.warning(f"Failed to decrypt {evt.event_id}: {e}")
                self.log.trace("%s decryption traceback:", evt.event_id, exc_info=True)
                msg = f"\u26a0 Your message was not bridged: {e}"
            else:
                self.send_decrypted_checkpoint(decrypted, retry_num=1)
                await self.az.intent.redact(evt.room_id, event_id)
                await self.int_handle_event(decrypted, send_bridge_checkpoint=False)
                return
        else:
            error_message = f"Didn't get {err.session_id}, giving up on {evt.event_id}"
            self.log.warning(error_message)
            self.send_decrypted_checkpoint(evt, error_message, True, retry_num=1)
            msg = (
                "\u26a0 Your message was not bridged: the bridge hasn't received the decryption"
                " keys. If this error keeps happening, try restarting your client."
            )
        content = TextMessageEventContent(msgtype=MessageType.NOTICE, body=msg)
        content.set_edit(event_id)
        await self.az.intent.send_message(evt.room_id, content)

    async def handle_encryption(self, evt: StateEvent) -> None:
        await self.az.state_store.set_encryption_info(evt.room_id, evt.content)
        portal = await self.bridge.get_portal(evt.room_id)
        if portal:
            portal.encrypted = True
            await portal.save()
            if portal.is_direct:
                portal.log.debug("Received encryption event in direct portal: %s", evt.content)
                await portal.enable_dm_encryption()

    def send_message_send_checkpoint(
        self,
        evt: Event,
        step: MessageSendCheckpointStep,
        err: Exception | str | None = None,
        permanent: bool = False,
        retry_num: int = 0,
    ) -> None:
        endpoint = self.bridge.config["homeserver.message_send_checkpoint_endpoint"]
        if not endpoint:
            return
        if evt.type not in CHECKPOINT_TYPES:
            return

        self.log.debug(f"Sending message send checkpoint for {evt.event_id} (step: {step})")
        status = MessageSendCheckpointStatus.SUCCESS
        if err:
            status = (
                MessageSendCheckpointStatus.PERM_FAILURE
                if permanent
                else MessageSendCheckpointStatus.WILL_RETRY
            )

        checkpoint = MessageSendCheckpoint(
            event_id=evt.event_id,
            room_id=evt.room_id,
            step=step,
            timestamp=int(time.time() * 1000),
            status=status,
            reported_by=MessageSendCheckpointReportedBy.BRIDGE,
            event_type=evt.type,
            message_type=evt.content.msgtype if evt.type == EventType.ROOM_MESSAGE else None,
            info=str(err) if err else None,
            retry_num=retry_num,
        )
        asyncio.create_task(checkpoint.send(self.log, endpoint, self.az.as_token))

    def send_bridge_checkpoint(self, evt: Event) -> None:
        self.send_message_send_checkpoint(evt, MessageSendCheckpointStep.BRIDGE)

    def send_decrypted_checkpoint(
        self,
        evt: Event,
        err: Exception | str | None = None,
        permanent: bool = False,
        retry_num: int = 0,
    ) -> None:
        self.send_message_send_checkpoint(
            evt, MessageSendCheckpointStep.DECRYPTED, err, permanent, retry_num
        )

    allowed_event_classes: tuple[type, ...] = (
        MessageEvent,
        StateEvent,
        ReactionEvent,
        EncryptedEvent,
        RedactionEvent,
        ReceiptEvent,
        TypingEvent,
        PresenceEvent,
    )

    async def allow_matrix_event(self, evt: Event) -> bool:
        # If the event is not one of the allowed classes, ignore it.
        if not isinstance(evt, self.allowed_event_classes):
            return False
        # For room events, make sure the message didn't originate from the bridge.
        if isinstance(evt, BaseRoomEvent):
            # If the event is from a bridge ghost, ignore it.
            if evt.sender == self.az.bot_mxid or self.bridge.is_bridge_ghost(evt.sender):
                return False
            # If the event is marked as double puppeted and we can confirm that we are in fact
            # double puppeting that user ID, ignore it.
            if (
                evt.content.get(DOUBLE_PUPPET_SOURCE_KEY) == self.az.bridge_name
                and await self.bridge.get_double_puppet(evt.sender) is not None
            ):
                return False
        # For non-room events and non-bridge-originated room events, allow.
        return True

    async def int_handle_event(self, evt: Event, send_bridge_checkpoint: bool = True) -> None:
        if isinstance(evt, StateEvent) and evt.type == EventType.ROOM_MEMBER and self.e2ee:
            await self.e2ee.handle_member_event(evt)
        if not await self.allow_matrix_event(evt):
            return
        self.log.trace("Received event: %s", evt)

        if send_bridge_checkpoint:
            self.send_bridge_checkpoint(evt)
        start_time = time.time()

        if evt.type == EventType.ROOM_MEMBER:
            evt: StateEvent
            unsigned = evt.unsigned or StateUnsigned()
            prev_content = unsigned.prev_content or MemberStateEventContent()
            prev_membership = prev_content.membership if prev_content else Membership.JOIN
            if evt.content.membership == Membership.INVITE:
                await self.int_handle_invite(
                    evt.room_id, UserID(evt.state_key), evt.sender, evt.event_id
                )
            elif evt.content.membership == Membership.LEAVE:
                if prev_membership == Membership.BAN:
                    await self.handle_unban(
                        evt.room_id,
                        UserID(evt.state_key),
                        evt.sender,
                        evt.content.reason,
                        evt.event_id,
                    )
                elif prev_membership == Membership.INVITE:
                    if evt.sender == evt.state_key:
                        await self.handle_reject(
                            evt.room_id, UserID(evt.state_key), evt.content.reason, evt.event_id
                        )
                    else:
                        await self.handle_disinvite(
                            evt.room_id,
                            UserID(evt.state_key),
                            evt.sender,
                            evt.content.reason,
                            evt.event_id,
                        )
                elif evt.sender == evt.state_key:
                    await self.handle_leave(evt.room_id, UserID(evt.state_key), evt.event_id)
                else:
                    await self.handle_kick(
                        evt.room_id,
                        UserID(evt.state_key),
                        evt.sender,
                        evt.content.reason,
                        evt.event_id,
                    )
            elif evt.content.membership == Membership.BAN:
                await self.handle_ban(
                    evt.room_id,
                    UserID(evt.state_key),
                    evt.sender,
                    evt.content.reason,
                    evt.event_id,
                )
            elif evt.content.membership == Membership.JOIN:
                if prev_membership != Membership.JOIN:
                    await self.handle_join(evt.room_id, UserID(evt.state_key), evt.event_id)
                else:
                    await self.handle_member_info_change(
                        evt.room_id, UserID(evt.state_key), evt.content, prev_content, evt.event_id
                    )
        elif evt.type in (EventType.ROOM_MESSAGE, EventType.STICKER):
            evt: MessageEvent
            if evt.type != EventType.ROOM_MESSAGE:
                evt.content.msgtype = MessageType(str(evt.type))
            await self.handle_message(evt.room_id, evt.sender, evt.content, evt.event_id)
        elif evt.type == EventType.ROOM_ENCRYPTED:
            await self.handle_encrypted(evt)
        elif evt.type == EventType.ROOM_ENCRYPTION:
            await self.handle_encryption(evt)
        else:
            if evt.type.is_state and isinstance(evt, StateEvent):
                await self.handle_state_event(evt)
            elif evt.type.is_ephemeral and isinstance(
                evt, (PresenceEvent, TypingEvent, ReceiptEvent)
            ):
                await self.handle_ephemeral_event(evt)
            else:
                await self.handle_event(evt)

        await self.log_event_handle_duration(evt, time.time() - start_time)

    async def log_event_handle_duration(self, evt: Event, duration: float) -> None:
        EVENT_TIME.labels(event_type=str(evt.type)).observe(duration)
