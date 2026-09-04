/**
 * The lifecycle of a creative director `client_turn_id`.
 *
 * The id belongs to the SEND ACTION, not to an HTTP attempt. Minting one per
 * attempt made the key useless exactly when it mattered: a send whose response
 * was lost - a timeout, a dropped connection, a 502 from the edge - came back
 * with a fresh id, so the server saw a brand new turn, wrote a second one and
 * charged for a second director call for words the user typed once.
 *
 * So: one id per (user, project, request slot, message text). It survives
 * every retryable failure and every reload of the tab, and it is released on
 * exactly two events - a definitive success, or a definitive terminal failure
 * (a refusal the server will give again no matter how often it is asked).
 * Editing the words is what mints a new one, because different words are a
 * different turn and the server refuses to replay a recorded reply for them.
 *
 * Everything here is pure: the caller owns the storage. That keeps the rule
 * testable outside a browser, which is where its bugs are invisible.
 */

/** How long a pending id stays usable. Longer than any believable retry. */
export const CREATIVE_TURN_MAX_AGE_MS = 24 * 60 * 60 * 1000;

/**
 * The isolation boundary, as one unambiguous string.
 *
 * Two accounts or two projects sharing a browser must never share a pending
 * id: replaying account A's key into account B's project would either 404 or,
 * worse, hand back a reply that belongs to someone else's conversation. The
 * JSON array form cannot be spoofed by a value that contains the separator.
 *
 * @param {{userId?: string|null, projectId?: string|null, slot?: string|null}} scope
 */
export function turnScopeKey({ userId, projectId, slot }) {
  return JSON.stringify([String(userId ?? ""), String(projectId ?? ""), String(slot ?? "")]);
}

/** Is `error` one the same request could still win by asking again? */
export function isRetryableSendFailure(error) {
  const status = Number(error?.status || 0);
  // No status at all: fetch itself rejected - DNS, TLS, a dropped socket, an
  // aborted timeout. The request may well have reached the server and been
  // committed, which is the whole reason the id has to survive.
  if (!status) return true;
  if (status >= 500) return true;
  if ([408, 425, 429].includes(status)) return true;
  // A refusal the director itself marked retryable (a superseded brief
  // revision, say) refused *before* writing anything, so the same words may be
  // sent again - and under the same id, since nothing was recorded for it.
  return error?.detail?.retryable === true;
}

/**
 * The id to send for this attempt.
 *
 * Reuses the pending id when the scope and the words both match; mints a new
 * one otherwise. Returns a new store - the input is never mutated.
 *
 * @param {object} pending  scope key -> {id, content, createdAt}
 * @param {string} scopeKey from `turnScopeKey`
 * @param {string} content  the exact text that will be sent
 * @param {() => string} mint  fresh id source
 * @param {number} now
 * @returns {{pending: object, id: string, reused: boolean}}
 */
export function beginCreativeTurn(pending, scopeKey, content, mint, now = Date.now()) {
  const store = pruneCreativeTurns(pending, now);
  const held = store[scopeKey];
  // Exact text, not a hash: the server compares the recorded turn's content
  // byte for byte, so a near-match must mint rather than collide into a 409.
  const reused = Boolean(held && held.id && held.content === content);
  const id = reused ? held.id : mint();
  return {
    pending: { ...store, [scopeKey]: { id, content, createdAt: reused ? held.createdAt : now } },
    id,
    reused,
  };
}

/**
 * Record what the attempt came back with.
 *
 * `"retryable"` keeps the id so the next attempt re-sends it; `"success"` and
 * `"terminal"` release it. A settle for an id that is no longer the pending
 * one is ignored, so a late reply from an abandoned attempt cannot free the
 * id a newer send is holding.
 *
 * @param {object} pending
 * @param {string} scopeKey
 * @param {string} id
 * @param {"success"|"terminal"|"retryable"} outcome
 */
export function settleCreativeTurn(pending, scopeKey, id, outcome) {
  const held = pending[scopeKey];
  if (!held || held.id !== id) return pending;
  if (outcome === "retryable") return pending;
  const next = { ...pending };
  delete next[scopeKey];
  return next;
}

/** Drop stale and malformed records. Also the reader's sanitiser. */
export function pruneCreativeTurns(pending, now = Date.now()) {
  const cutoff = now - CREATIVE_TURN_MAX_AGE_MS;
  const entries = Object.entries(pending || {}).filter(([, item]) => (
    item && typeof item.id === "string" && item.id
    && typeof item.content === "string"
    && Number(item.createdAt || 0) >= cutoff
  ));
  return Object.fromEntries(entries);
}
