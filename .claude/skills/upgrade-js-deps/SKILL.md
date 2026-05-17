---
name: upgrade-js-deps
description: Upgrade JavaScript/npm dependencies, then run type-check, build, and test to ensure nothing is broken.
allowed-tools: [
    Bash(npx npm-check-updates *),
    Bash(make npm-install *),
    Bash(make npm-type-check *),
    Bash(make npm-build *),
    Bash(make test *),
]
---

## Your task

Upgrade all JavaScript dependencies and verify nothing is broken.

### Step 1: Check available upgrades

Run `npx npm-check-updates` to show what packages have new versions available.
Present the list to the user and ask how they'd like to proceed before making changes.

### Step 2: Update package.json

Run `npx npm-check-updates -u` to update all version ranges in `package.json`.

### Step 3: Install upgraded packages

Run `make npm-install` to fetch the upgraded packages and update the lock file.

### Step 4: Run post-upgrade checks

Run these checks sequentially, stopping immediately if any step fails:
- **Type safety**: Run `make npm-type-check` and report any new TypeScript errors.
  These may be caused by updated type definitions or changed library APIs.
- **Build verification**: Run `make npm-build` to ensure the production build still compiles.
- **Test suite**: Run `make test` to confirm all tests still pass.

### Step 5: Summarize and commit

Summarize what was done:
- Which packages were upgraded and their version changes
- Any newly introduced type errors
- Build success/failure
- Test pass/fail results
- Any issues requiring manual intervention

If there were failures, report them and ask how the user wants to proceed.

If everything passed, ask the user if they'd like to commit the changes.
