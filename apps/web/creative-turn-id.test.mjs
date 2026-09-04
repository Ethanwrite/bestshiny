/**
 * The client_turn_id lifecycle, tested where its bugs actually live.
 *
 * Run:  node --test apps/web/creative-turn-id.test.mjs
 *
 * apps/web has no test runner and no DOM harness, so the rule was pulled out
 * of app.js into a pure module: no fetch, no storage, no window. What is
 * pinned here is exactly the defect - a retry minting a fresh id - and its
 * three boundaries: what releases an id, what keeps it, and what may never
 * share one.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  beginCreativeTurn,
  isRetryableSendFailure,
  pruneCreativeTurns,
  settleCreativeTurn,
  turnScopeKey,
} from "./creative-turn-id.js";

/** A mint that is obviously distinguishable per call. */
function counter(prefix = "id") {
  let n = 0;
  return () => `${prefix}-${++n}`;
}

const SCOPE = turnScopeKey({ userId: "user-1", projectId: "project-1", slot: "session:s1" });

test("the id is minted once for the send action, not once per attempt", () => {
  const mint = counter();
  const first = beginCreativeTurn({}, SCOPE, "再悬疑一点", mint);
  assert.equal(first.id, "id-1");
  assert.equal(first.reused, false);

  // The connection dropped: no status at all, so the server may already have
  // committed the turn. The id has to survive that.
  assert.equal(isRetryableSendFailure(new TypeError("Failed to fetch")), true);
  const held = settleCreativeTurn(first.pending, SCOPE, first.id, "retryable");

  const retry = beginCreativeTurn(held, SCOPE, "再悬疑一点", mint);
  assert.equal(retry.id, "id-1", "the retry must carry the original id");
  assert.equal(retry.reused, true);
  assert.equal(mint(), "id-2", "the retry did not consume a fresh id");
});

test("5xx and timeouts keep the id; a definitive refusal releases it", () => {
  for (const status of [500, 502, 503, 504, 408, 425, 429]) {
    assert.equal(isRetryableSendFailure({ status }), true, `${status} must retry with the same id`);
  }
  for (const status of [400, 401, 403, 404, 409, 422]) {
    assert.equal(isRetryableSendFailure({ status }), false, `${status} must not be retried blindly`);
  }
  // A 409 the director itself marked retryable refused before writing.
  assert.equal(
    isRetryableSendFailure({ status: 409, detail: { reason_code: "BRIEF_REVISION_CHANGED", retryable: true } }),
    true,
  );
  assert.equal(
    isRetryableSendFailure({ status: 409, detail: { reason_code: "CLIENT_TURN_ID_CONTENT_MISMATCH" } }),
    false,
  );

  const mint = counter();
  const begun = beginCreativeTurn({}, SCOPE, "words", mint);
  const afterTerminal = settleCreativeTurn(begun.pending, SCOPE, begun.id, "terminal");
  assert.deepEqual(afterTerminal, {});
  assert.equal(beginCreativeTurn(afterTerminal, SCOPE, "words", mint).id, "id-2");
});

test("success releases the id, so the next send is a new turn", () => {
  const mint = counter();
  const begun = beginCreativeTurn({}, SCOPE, "same words", mint);
  const settled = settleCreativeTurn(begun.pending, SCOPE, begun.id, "success");
  assert.deepEqual(settled, {});
  const next = beginCreativeTurn(settled, SCOPE, "same words", mint);
  assert.equal(next.id, "id-2", "repeating yourself on purpose is a second turn, not a replay");
  assert.equal(next.reused, false);
});

test("only editing the words mints a new id", () => {
  const mint = counter();
  const begun = beginCreativeTurn({}, SCOPE, "make it darker", mint);
  const held = settleCreativeTurn(begun.pending, SCOPE, begun.id, "retryable");

  assert.equal(beginCreativeTurn(held, SCOPE, "make it darker", mint).id, "id-1");
  const edited = beginCreativeTurn(held, SCOPE, "make it darker and shorter", mint);
  assert.equal(edited.id, "id-2");
  assert.equal(edited.reused, false);
  // Whitespace is a different message to the server, which compares content
  // exactly - so it must mint here rather than collide into a 409.
  assert.notEqual(beginCreativeTurn(held, SCOPE, "make it darker ", mint).id, "id-1");
});

test("no two users, projects or slots ever share a pending id", () => {
  const mint = counter();
  const mine = turnScopeKey({ userId: "user-1", projectId: "project-1", slot: "session:new" });
  const otherUser = turnScopeKey({ userId: "user-2", projectId: "project-1", slot: "session:new" });
  const otherProject = turnScopeKey({ userId: "user-1", projectId: "project-2", slot: "session:new" });
  const otherSlot = turnScopeKey({ userId: "user-1", projectId: "project-1", slot: "session:s1" });
  assert.equal(new Set([mine, otherUser, otherProject, otherSlot]).size, 4);

  const begun = beginCreativeTurn({}, mine, "an idea", mint);
  const held = settleCreativeTurn(begun.pending, mine, begun.id, "retryable");
  for (const scope of [otherUser, otherProject, otherSlot]) {
    assert.equal(beginCreativeTurn(held, scope, "an idea", mint).reused, false);
  }
  // The separator cannot be forged from inside a value.
  assert.notEqual(
    turnScopeKey({ userId: "a", projectId: "b", slot: "c" }),
    turnScopeKey({ userId: 'a","b', projectId: "", slot: "c" }),
  );
});

test("a late settle from an abandoned attempt cannot free a newer id", () => {
  const mint = counter();
  const first = beginCreativeTurn({}, SCOPE, "one", mint);
  const held = settleCreativeTurn(first.pending, SCOPE, first.id, "retryable");
  const second = beginCreativeTurn(held, SCOPE, "two", mint);
  const stale = settleCreativeTurn(second.pending, SCOPE, first.id, "success");
  assert.equal(stale[SCOPE].id, second.id);
});

test("stale and malformed records are dropped, not sent", () => {
  const now = Date.parse("2026-09-03T00:00:00Z");
  const pending = {
    fresh: { id: "keep", content: "x", createdAt: now - 1000 },
    old: { id: "drop", content: "x", createdAt: now - (25 * 60 * 60 * 1000) },
    broken: { content: "no id", createdAt: now },
    junk: null,
  };
  assert.deepEqual(Object.keys(pruneCreativeTurns(pending, now)), ["fresh"]);
  assert.deepEqual(pruneCreativeTurns(undefined), {});
});

test("beginCreativeTurn never mutates the store it was given", () => {
  const pending = Object.freeze({});
  assert.doesNotThrow(() => beginCreativeTurn(pending, SCOPE, "x", counter()));
  assert.deepEqual(pending, {});
});

test("a pending id is forgotten once its turn is visible on the record", () => {
  // Mirrors app.js's releaseLandedCreativeTurns: the store is keyed by scope
  // and each entry carries the id, so a turn the conversation already shows
  // settles its entry and the next identical message mints a new id.
  const scope = turnScopeKey({ userId: "u", projectId: "p", slot: "session:s" });
  let pending = beginCreativeTurn({}, scope, "再短一点", () => "id-1").pending;
  assert.equal(pending[scope].id, "id-1");

  const turns = [{ speaker: "USER", client_turn_id: "id-1" }];
  const landed = new Set(turns.filter((t) => t.speaker === "USER" && t.client_turn_id).map((t) => t.client_turn_id));
  for (const [key, entry] of Object.entries(pending)) {
    if (landed.has(entry.id)) delete pending[key];
  }
  assert.deepEqual(pending, {});

  // The same words sent again are a new send, not a replay of the old reply.
  const again = beginCreativeTurn(pending, scope, "再短一点", () => "id-2");
  assert.equal(again.id, "id-2");
});
