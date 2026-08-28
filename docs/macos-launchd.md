# Run as a macOS LaunchAgent

The repository includes a conservative localhost-only LaunchAgent template.

1. Clone the repository to a stable absolute path.
2. Copy `deploy/com.example.aihot-review.plist` to
   `~/Library/LaunchAgents/com.example.aihot-review.plist`.
3. Replace every `REPLACE_WITH_*` placeholder with an absolute path. XML plist
   files do not expand `~` or shell variables.
4. Validate and load it:

   ```bash
   plutil -lint ~/Library/LaunchAgents/com.example.aihot-review.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.aihot-review.plist
   launchctl kickstart -k gui/$(id -u)/com.example.aihot-review
   ```

5. Verify the process and the functional endpoint:

   ```bash
   launchctl print gui/$(id -u)/com.example.aihot-review
   curl -fsS http://127.0.0.1:8765/api/health
   ```

To unload it:

```bash
launchctl bootout gui/$(id -u)/com.example.aihot-review
```

The template starts without `--pull`, so a temporary upstream outage does not
put the LaunchAgent into a restart loop. Use the UI's `Pull Latest` button, or
add `--pull --hours 24` after deciding that startup should depend on upstream
availability.
