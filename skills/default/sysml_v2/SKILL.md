---
name: sysmlv2-skill
description: SysML v2 systems modeling guidance for creating valid models with correct syntax, proper type hierarchies, and validation workflows. Use when working with .sysml files, systems modeling, architecture documentation, SysON, or Eclipse SysML tools. Provides syntax reference, common patterns, error troubleshooting, and best practices from OMG specifications.
license: ISC
allowed-tools: Bash Read Edit Write Grep Glob WebFetch
metadata:
  version: "1.0.0"
  author: sysmlv2-skill contributors
---

# SysML v2 Systems Modeling

Authoritative guidance for creating valid SysML v2 models with correct
syntax, proper type hierarchies, and integration with validation
tools.

## Core Principle

**SysML v2 is a textual modeling language with formal syntax.** Always
validate models with the command-line validator before importing into
SysON or committing to version control. The language is defined by OMG
specifications, implemented by Eclipse tooling, and must follow strict
KerML (Kernel Modeling Language) metamodel rules.

## Essential References

**Primary Sources** (in order of authority):

1. **OMG SysML v2 Specification**: https://www.omg.org/spec/SysML/2.0/
   - Official language specification
   - Normative example model: https://www.omg.org/cgi-bin/doc?ptc/25-04-31.sysml

2. **SysML v2 Pilot Implementation**: https://github.com/Systems-Modeling/SysML-v2-Pilot-Implementation
   - Reference implementation with parser and validator

3. **SysML v2 Release Repository**: https://github.com/Systems-Modeling/SysML-v2-Release
   - Standard library models included in the pilot implementation

4. **Eclipse SysON**: https://doc.mbse-syson.org/ (web-based graphical modeling)

**For detailed syntax rules**: See [references/SYNTAX.md](references/SYNTAX.md)
**For error troubleshooting**: See [references/ERRORS.md](references/ERRORS.md)
**For common patterns**: See [references/PATTERNS.md](references/PATTERNS.md)

## Validation Workflow

**CRITICAL: Always validate before committing or importing to SysON**

Use the `validate-sysml` command from
[sysmlv2-validator](https://github.com/decisym/sysmlv2-validator):

```sh
validate-sysml model.sysml
```

Errors are reported in GNU format for editor integration:

```
filename:line:column: error: message
```

## Top 3 Critical Rules

1. **Always add visibility modifier to imports**: `private import
   ScalarValues::*;` (NOT `import ScalarValues::*;`)
2. **Attribute defs must specialize**: `attribute def Region :>
   String;` (NOT `attribute def Region;`)
3. **Import ScalarValues for basic types**: String, Real, Integer,
   Boolean all require this import

**For complete syntax reference**: See `references/SYNTAX.md`
**For error troubleshooting**: See `references/ERRORS.md`
**For common patterns**: See `references/PATTERNS.md`

## Integration Workflows

**SysON (visual modeling)**: Deploy via Docker (`docker run -p
8080:8080 eclipsesyson/syson:latest`) or see
https://doc.mbse-syson.org/

**Infrastructure-as-Code**: Model in SysML → Validate → Implement in
Terraform/Pulumi → Verify match
