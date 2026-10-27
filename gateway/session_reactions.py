"""Desktop's message reactions (``message.react``) on the owner's transcript.

The sidecar handler looked the session up in its own table, which never holds an authority
session, and the ``message.*`` namespace never falls back to it, so every reaction was rolled back
as "Could not react". The write is the same ``SessionDB.set_message_reaction`` (Tapback semantics,
rows of the session's visible resume lineage only), on the conversation's current physical session.

The same file owns the ANNOUNCEMENT: a user's reactions reach the model as a note on the next user
turn (never by rewriting the reacted message, which would cost the cached prefix). Both turn lanes
spell it here — the TUI gateway (``tui_gateway.methods_prompt``) and the canonical gateway's LOCAL
turns — so the wording and the once-only rule cannot drift.
"""
import asyncio
import logging
from functools import partial

from gateway.session_busy_controls import authorize
from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)

_FIELDS = frozenset({'session_id', 'profile', 'row_id', 'newest_role', 'emoji', 'author'})


def _parse(params):
    row_id, newest_role, emoji = params.get('row_id'), params.get('newest_role'), params.get('emoji')
    author = params.get('author') or 'user'
    if (set(params) - _FIELDS or (row_id is not None and type(row_id) is not int)
            or (row_id is None and newest_role not in {'user', 'assistant'})
            or (emoji is not None and (not isinstance(emoji, str) or not emoji.strip()))
            or author not in {'user', 'agent'}):
        raise RuntimeStoreError('invalid_params')
    return row_id, newest_role, emoji.strip() if emoji else None, author


def _write(db, target, row_id, newest_role, emoji, author):
    if row_id is None:
        row_id = db.latest_message_row_id(target, role=newest_role)
        if row_id is None:
            return None, None
    return row_id, db.set_message_reaction(target, row_id, emoji, author=author)


async def react(connection, ref, params):
    row_id, newest_role, emoji, author = _parse(params)
    authority = connection.authority
    if not ref.session_id or ref.session_id not in authority.sessions:
        raise RuntimeStoreError('not_found')
    authorize(connection, ref, params, 'session:submit')
    from gateway.session_ancillary import physical_target
    from gateway.session_runtime_workers import tracked_write
    # A reset or compression moves the conversation's rows onto a new physical session; the
    # reaction lineage check runs from that tip, as the sidecar's submit-row target did.
    target = physical_target(authority, ref)
    row_id, reactions = await tracked_write(authority, partial(
        _write, authority.db, target, row_id, newest_role, emoji, author))
    if reactions is None:
        raise RuntimeStoreError('not_found')
    return {'row_id': row_id, 'reactions': reactions}


# ── announcement: the next user turn's note ────────────────────────────────

def reaction_notes(rows) -> str:
    """Model-input note block for reactions taken since the last turn, or "".

    Model input only: nothing is written to the transcript and no sent message is rewritten, so the
    cached prefix survives (the reacted row keeps its original content and just gains ``seen``).
    """
    notes = []
    for entry in rows or ():
        snippet = (entry.get('text') or '').strip().replace('\n', ' ')
        if len(snippet) > 120:
            snippet = snippet[:120] + '…'
        emoji = entry.get('emoji') or ''
        whose = 'their own' if entry.get('role') == 'user' else 'your'
        if snippet:
            notes.append(f'[The user reacted {emoji} to {whose} message: "{snippet}"]')
        else:
            # Attachment-only / tool-call-only rows: no quote beats an empty quote.
            notes.append(f'[The user reacted {emoji} to {whose} earlier message]')
    return '\n'.join(notes)


def reactions_announced(user_config) -> bool:
    """``display.message_reactions`` — the opt-in every announcing lane gates on."""
    display = user_config.get('display') if isinstance(user_config, dict) else None
    return bool(isinstance(display, dict) and display.get('message_reactions', False))


async def pending_reaction_notes(runner, source, session_key: str) -> str:
    """The note block a LOCAL turn prepends for reactions since the last turn, or "".

    A Desktop conversation served by the canonical gateway runs no TUI prompt lane, so nothing else
    surfaces these rows there (#145472). Messaging platforms keep their own reaction events — their
    adapters announce a reaction as the event it is, and a second note would duplicate it.
    Gated BEFORE the read: a disabled announcement must not consume the reactions it never shows.
    """
    from gateway.config import Platform
    if getattr(source, 'platform', None) is not Platform.LOCAL or not session_key:
        return ''
    try:
        from hermes_cli.config import load_config_readonly
        if not reactions_announced(await asyncio.to_thread(load_config_readonly)):
            return ''
    except Exception:  # health: allow BLE001 -- an unreadable config must never fail a user turn
        logger.debug('Failed to read display.message_reactions', exc_info=True)
        return ''
    store = getattr(runner, 'session_store', None)
    try:
        session_id = store.peek_session_id(session_key) if store is not None else None
    except Exception:  # health: allow BLE001 -- key -> id lookup is advisory here, never fatal
        logger.debug('Failed to resolve the session id for reaction notes', exc_info=True)
        return ''
    db = getattr(runner, '_session_db', None)
    if not session_id or db is None:
        return ''
    try:
        return reaction_notes(await db.take_unseen_reactions(session_id, author='user'))
    except Exception:  # health: allow BLE001 -- an unreadable store must never fail a user turn
        logger.debug('Failed to read pending reactions', exc_info=True)
        return ''

