# Native eventMacro support

KoreLab treats OpenKore's `eventMacro` plugin as the native executor. It does not bundle a replacement macro engine or require scripts from any historical milestone.

Provide a source file in a scenario:

```json
{
  "openkore": {
    "eventmacro_source": "/absolute/path/to/my-eventmacros.txt"
  }
}
```

Resolve the source path to an absolute path before running; scenario source paths are consumed as provided. KoreLab copies the file to a private run-local control directory, points OpenKore's `eventMacro_file` setting at that copy, and enables the native plugin. This keeps the caller's source and original OpenKore profile unchanged.

The OpenKore adapter supports replacing a loaded eventMacro source during a run and verifies expected macro names from the native plugin's listing. Native commands and logs provide execution evidence. A successful load or command alone is not proof that a macro achieved its intended game-state result; define and observe scenario postconditions separately.

No eventMacro source is bundled. Regression tests generate small caller-like macro files in temporary directories, while the runtime accepts caller-provided source paths. The OpenKore `eventMacro` plugin and a compatible OpenKore installation remain prerequisites for native execution.
