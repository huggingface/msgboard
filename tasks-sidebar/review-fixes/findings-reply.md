Addressed the six findings from [review #1](https://github.com/huggingface/msgboard/pull/56#issuecomment-7194221818) in **`602c5b7`** (main fix commit `cc871f4`, plus pagination-control refresh `602c5b7`):

1. **Empty DM:** its cursor comes from actual returned message sequences, retaining zero for an empty conversation. The real browser test sends the first message and receives the first agent reply in a new DM.
2. **Late Settings response:** each tab renderer checks that it still owns the active tab/body before touching the DOM; the error renderer has the same guard. Tested holding Invites, switching to Personal, then releasing the reply and navigating again.
3. **Unseen edits:** content updates preserve `seen`. The API test now edits before human acknowledgment and confirms the task remains unread.
4. **Covered rows:** task-row acknowledgment is suspended while the Agent pane overlays the list. Revealing the list observes it again. Tested a fresh unseen task behind the pane at 900 px, then closing the pane.
5. **Deleted messages:** live refresh now reconciles the loaded history range through authoritative, paged reads. Deleted rows are removed and older loaded pages stay present. Tested deleting both an older loaded message and the latest original message, followed by a new reply.
6. **Rapid switching:** the departing session is detached before displaying Loading. Tested A → held B → A, retaining A’s 700 px position and ignoring B’s late response.

No new test functions or schema changes in this follow-up; existing Tasks tests were extended. **20 targeted tests passed in 54.39 seconds.**

**Complete final-head offline suite: 274 passed, one known baseline phone-swipe failure, one live test excluded, 261.80 seconds** (two serial file batches). The earlier single-process attempt was interrupted before a summary; the two completed batches cover all test files without overlap. The phone-swipe failure is unchanged from the independently reproduced main baseline. JavaScript syntax, fatal-error Ruff and whitespace checks pass.

Please recheck this head and update the existing review in place. No merge or deployment.
