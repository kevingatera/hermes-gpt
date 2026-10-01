# Live Hermes interaction

The plugin should support ordinary Hermes work from ChatGPT, including actions,
follow-up requests, configuration changes, and failure diagnosis. A tool list
that disappears whenever a feature is disabled prevents that workflow.

## Implementation

1. Keep a stable management and session catalogue on explicitly administered
   private connections. Evaluate feature and profile permissions per call.
2. Add validated, persistent connection settings. Limit them to known feature
   switches, profiles within a server-authorized ceiling, and apply mode.
   Preserve secret-path restrictions, fixed commands, and Owner boundaries.
3. Expose profile configuration and scheduler mutations with explicit action
   confirmation and dry-run previews. Profile defaults apply on the next turn.
4. Describe live reconfiguration in server instructions, the plugin skill, and
   the panel. Existing settings changes need no tool rediscovery. New tool
   schemas still require a client metadata refresh after deployment.
5. Test state transitions through one initialized MCP connection, real file
   mutations in disposable fixtures, and representative Hermes workflows.
6. Check private operational evidence locally. Publish tests and behavior,
   not personal account data, transcripts, or scheduled prompt bodies.
