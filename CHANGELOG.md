# Changelog

All notable changes to this project will be documented in this file.

## 0.2.0 - 2026-08-28

- Extracted the Miner review app into a standalone GitHub-ready project.
- Added configurable data, Miner tree, and upstream endpoint paths.
- Preserved automatic Miner integration when the app runs in-tree.
- Added atomic JSON snapshot writes and serialized JSONL appends.
- Added a local health endpoint, Docker setup, launchd template, CI, tests, and
  security/documentation files.
- Kept the inbox snapshot cache and in-memory decision/view updates that avoid
  rescanning every historical run after each label action.
