Addressed review #2’s remaining **#7** and **#8** in **`ddcd438`**.

- **#7:** `tasksRenderDm()` returns the response-time bottom decision; the new-message control uses that same result. The browser test holds a history request that started at the bottom, scrolls up, then releases the new reply and checks that the control appears while the reading position stays put.
- **#8:** a capture-phase native `close` listener reconnects task-row observation for every dialog, including Cancel/Escape. The test releases a delayed Tasks response under New channel, confirms the covered item remains unread, then cancels the dialog and checks that the revealed task is acknowledged.

**Both existing browser tests pass (45.09 s), including all earlier UI regressions.** No new test functions or schema changes. JavaScript syntax, fatal-error Ruff and whitespace checks pass. The full suite was not repeated for these two UI timing fixes; the complete `602c5b7` run remains 274 passed, one known baseline phone-swipe failure, one live test excluded.

Please recheck this head and update the same AI review comment in place. Not merged or deployed.
