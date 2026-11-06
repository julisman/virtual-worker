# Virtual Worker Template Manifest Schema

<!-- template-lint-version: 0.1.0 -->

Generated from `backend/storage/template_manifest.py`; do not edit by hand.

```json
{
  "connector": {
    "name": {
      "required": true,
      "type": "string"
    },
    "overrides": {
      "default": {},
      "type": "object"
    },
    "preset": {
      "required": true,
      "type": "string"
    },
    "secretBindings": {
      "default": {},
      "type": "object"
    }
  },
  "customTool": {
    "allowPrivate": {
      "default": false,
      "type": "boolean"
    },
    "description": {
      "default": "",
      "type": "string"
    },
    "fixedParams": {
      "default": {},
      "type": "object"
    },
    "headers": {
      "default": {},
      "type": "object"
    },
    "key": {
      "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
      "required": true,
      "type": "string"
    },
    "method": {
      "default": "GET",
      "type": "string"
    },
    "name": {
      "required": true,
      "type": "string"
    },
    "parameters": {
      "default": {
        "properties": {},
        "type": "object"
      },
      "type": "object"
    },
    "timeout": {
      "default": 20,
      "type": "integer"
    },
    "transport": {
      "default": "http",
      "type": "string"
    },
    "url": {
      "default": "",
      "type": "string"
    }
  },
  "knowledgeBase": {
    "description": {
      "default": "",
      "type": "string"
    },
    "embeddingModel": {
      "default": "",
      "type": "string"
    },
    "name": {
      "required": true,
      "type": "string"
    },
    "sourceFolderVariable": {
      "optional": true,
      "type": "string"
    }
  },
  "mcpServer": {
    "hint": {
      "default": "",
      "type": "string"
    },
    "mcpToolAllow": {
      "items": {
        "type": "string"
      },
      "optional": true,
      "type": "array"
    },
    "name": {
      "required": true,
      "type": "string"
    },
    "transport": {
      "default": "",
      "type": "string"
    }
  },
  "v1TopLevel": [
    "author",
    "category",
    "commit",
    "description",
    "digest",
    "guardrails",
    "id",
    "installedAt",
    "model",
    "name",
    "persona",
    "role",
    "schemaVersion",
    "skills",
    "source",
    "sourceId",
    "starterTasks",
    "temperature",
    "templateId",
    "tools",
    "topK",
    "topP",
    "variables",
    "version"
  ],
  "v2TopLevel": [
    "author",
    "category",
    "commit",
    "connectors",
    "customTools",
    "description",
    "digest",
    "guardrails",
    "id",
    "installedAt",
    "knowledgeBases",
    "mcpServers",
    "metadata",
    "model",
    "name",
    "persona",
    "requiresFeatures",
    "role",
    "schemaVersion",
    "skills",
    "source",
    "sourceId",
    "starterTasks",
    "temperature",
    "templateId",
    "tools",
    "topK",
    "topP",
    "variables",
    "version",
    "wikis"
  ],
  "variable": {
    "default": {
      "default": ""
    },
    "help": {
      "default": "",
      "type": "string"
    },
    "key": {
      "required": true,
      "type": "string"
    },
    "label": {
      "default": "<key>",
      "type": "string"
    },
    "required": {
      "default": false,
      "type": "boolean"
    },
    "type": {
      "default": "text",
      "enum": [
        "text",
        "folder",
        "model",
        "skill",
        "secret",
        "enum"
      ],
      "type": "string"
    }
  },
  "wiki": {
    "name": {
      "required": true,
      "type": "string"
    },
    "pathVariable": {
      "optional": true,
      "type": "string"
    },
    "repoUrl": {
      "required": true,
      "type": "string"
    },
    "wikiRoot": {
      "default": ".",
      "type": "string"
    }
  }
}
```
