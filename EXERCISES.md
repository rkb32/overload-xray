# Exercises

Parked learning tasks. Do them when you have time; each is small and self-contained.

## 1. Write the Renovate automerge rule yourself (parked 2026-10-06)

**File:** `renovate.json` → `packageRules`

The rule I wrote for you (minor+patch automerge, majors wait) is the core policy
of the whole migration. To actually learn it, redo it from scratch:

1. Delete the `"Automerge minor and patch updates..."` packageRule from `renovate.json`.
2. Re-add it yourself from memory: which two fields does it need?
3. Verify: run `npx --yes --package renovate renovate-config-validator renovate.json`.
4. Stretch: add a second rule that sets `"automerge": false` explicitly for
   `"matchUpdateTypes": ["major"]`. Is it redundant? Why might you still want it?
   (Hint: what happens if someone later changes `extends` to a preset that
   enables automerge globally?)
