# SysML v2 Common Errors and Fixes

Comprehensive catalog of validation errors with solutions.

## Table of Contents

- [Error: "mismatched input 'import' expecting '}'"](#error-mismatched-input-import-expecting-)
- [Error: "Couldn't resolve reference to Classifier 'String'"](#error-couldnt-resolve-reference-to-classifier-string)
- [Error: "Must directly or indirectly specialize Base::DataValue"](#error-must-directly-or-indirectly-specialize-basedatavalue)
- [Error: "Must directly or indirectly specialize Parts::Part"](#error-must-directly-or-indirectly-specialize-partspart)
- [Error: "no viable alternative at input '::'"](#error-no-viable-alternative-at-input-)
- [Error: "Couldn't resolve reference to Type 'PackageName'"](#error-couldnt-resolve-reference-to-type-packagename)
- [Error: "Couldn't resolve reference to Feature 'attributeName'"](#error-couldnt-resolve-reference-to-feature-attributename)
- [Error: "extraneous input 'keyword' expecting '}'"](#error-extraneous-input-keyword-expecting-)
- [Error: "mismatched input '=' expecting ':'"](#error-mismatched-input--expecting-)
- [Error: "mismatched input ';' expecting '{'"](#error-mismatched-input--expecting--1)
- [Error: "Multiplicity must be literal Integer or unbounded (*)"](#error-multiplicity-must-be-literal-integer-or-unbounded-)
- [Error: "Feature must have a type"](#error-feature-must-have-a-type)
- [Validation Best Practices](#validation-best-practices)
- [Quick Fixes Checklist](#quick-fixes-checklist)
- [Reference](#reference)

## Error: "mismatched input 'import' expecting '}'"

**Cause**: Missing visibility modifier on import statement

**Wrong**:
```sysml
package MyPackage {
    import ScalarValues::*;  // Missing visibility modifier
}
```

**Correct**:
```sysml
package MyPackage {
    private import ScalarValues::*;
}
```

**Fix**: Add `private` or `public` before `import`.

## Error: "Couldn't resolve reference to Classifier 'String'"

**Cause**: Missing import of ScalarValues library

**Wrong**:
```sysml
package MyPackage {
    attribute def Name :> String;  // String not imported
}
```

**Correct**:
```sysml
package MyPackage {
    private import ScalarValues::*;

    attribute def Name :> String;
}
```

**Fix**: Add `private import ScalarValues::*;` at the top of your package.

## Error: "Must directly or indirectly specialize Base::DataValue"

**Cause**: Attribute definition doesn't specialize a data type

**Wrong**:
```sysml
attribute def Region;  // No specialization
```

**Correct**:
```sysml
attribute def Region :> String;
```

**Fix**: Add `:> String` (or `:> Real`, `:> Integer`, `:> Boolean`) to specify the base type.

## Error: "Must directly or indirectly specialize Parts::Part"

**Cause**: Part definition appears to not specialize Parts::Part (usually due to syntax error)

**Wrong**:
```sysml
part def MyComponent;  // No body or attributes
```

**Correct**:
```sysml
// Option 1: Add a body with at least one member
part def MyComponent {
    attribute name : String;
}

// Option 2: Explicitly specialize another part
part def MyComponent :> BaseComponent {
    attribute name : String;
}
```

**Fix**: Part definitions need a body with content, or explicit specialization.

## Error: "no viable alternative at input '::'"

**Cause**: Using `::` without proper context or missing visibility modifier

**Wrong**:
```sysml
import ScalarValues::*;  // Missing visibility
```

**Correct**:
```sysml
private import ScalarValues::*;
```

**Fix**: Add visibility modifier before import.

## Error: "Couldn't resolve reference to Type 'PackageName'"

**Cause**: Package or type referenced doesn't exist or isn't imported

**Wrong**:
```sysml
package MyPackage {
    private import NonExistentPackage::*;  // Package doesn't exist
}
```

**Correct**:
```sysml
package MyPackage {
    private import ScalarValues::*;  // Valid package from standard library
}
```

**Fix**:
1. Check spelling of package name
2. Ensure package exists in standard library or earlier in file
3. Verify import path is correct

## Error: "Couldn't resolve reference to Feature 'attributeName'"

**Cause**: Attribute or feature doesn't exist in the type being referenced

**Wrong**:
```sysml
part myApp : CodeEngineApp {
    attribute nonexistentAttr = "value";  // Attribute not defined in CodeEngineApp
}
```

**Correct**:
```sysml
// First define the attribute in the type
part def CodeEngineApp {
    attribute name : String;
}

// Then use it in instance
part myApp : CodeEngineApp {
    attribute name = "my-app";
}
```

**Fix**: Ensure the attribute is defined in the part definition before using it in an instance.

## Error: "extraneous input 'keyword' expecting '}'"

**Cause**: Keyword used in wrong context or syntax error

**Common causes**:
- Using `enum` keyword (not supported in SysML v2)
- Missing semicolon or brace
- Invalid nesting

**Wrong**:
```sysml
enum def Region {  // enum not supported
    US_EAST, US_SOUTH
}
```

**Correct**:
```sysml
attribute def Region :> String;
```

**Fix**: Check for unsupported keywords and proper syntax.

## Error: "mismatched input '=' expecting ':'"

**Cause**: Using `=` for typing instead of `:`, or vice versa

**Wrong**:
```sysml
// Using = for typing (should be :)
attribute name = String;
```

**Correct**:
```sysml
// Use : for typing
attribute name : String;

// Use = for value assignment
attribute name = "my-value";

// Or both
attribute name : String = "default-value";
```

**Fix**:
- Use `:` to specify type
- Use `=` to assign value

## Error: "mismatched input ';' expecting '{'"

**Cause**: Missing body for definition that requires one

**Wrong**:
```sysml
part def MyPart;  // Missing body
```

**Correct**:
```sysml
part def MyPart {
    attribute name : String;
}
```

**Fix**: Add curly braces with content.

## Error: "Multiplicity must be literal Integer or unbounded (*)"

**Cause**: Using variable or expression for multiplicity

**Wrong**:
```sysml
part items : Item[someVariable];  // Variable not allowed
```

**Correct**:
```sysml
part items : Item[0..*];    // Literal integers or *
part several : Item[2..5];   // Literal range
```

**Fix**: Use only literal integers and `*` for multiplicity.

## Error: "Feature must have a type"

**Cause**: Feature (attribute, part, etc.) declared without type

**Wrong**:
```sysml
part def Container {
    attribute name;  // Missing type
}
```

**Correct**:
```sysml
part def Container {
    attribute name : String;
}
```

**Fix**: Add `: TypeName` to specify the type.

## Validation Best Practices

### Run Validator After Every Change

```bash
./validate-sysml.sh my-model.sysml && echo "Valid!"
```

### Validate Files in Dependency Order

```bash
# Validate dependencies first
./validate-sysml.sh value-types.sysml

# Then files that import them
./validate-sysml.sh resource-library.sysml

# Then files that import the library
./validate-sysml.sh deployment.sysml
```

### Check Standard Library for Syntax Examples

```bash
cd ~/opt/jupyter-sysml-kernel-0.52.1/sysml/sysml.library

# Find examples of specific constructs
grep -r "attribute def" "Systems Library/"
grep -r "part def" "Systems Library/"
grep -r "connection def" "Systems Library/"
```

### Read Error Messages Carefully

Error format: `filename:line:column: error: message`

```
ibm-cloud-library.sysml:15:5: error: Couldn't resolve reference to Classifier 'String'
```

This means:
- File: `ibm-cloud-library.sysml`
- Line: 15
- Column: 5
- Issue: Type `String` cannot be resolved (missing import)

### Use Official Examples

Check the normative example model: https://www.omg.org/cgi-bin/doc?ptc/25-04-31.sysml

This file contains correct syntax for all language features.

## Quick Fixes Checklist

When you encounter an error:

1. ✅ Check for missing visibility modifier on imports
2. ✅ Verify `ScalarValues::*` is imported
3. ✅ Ensure attribute defs have `:> BaseType`
4. ✅ Confirm part defs have bodies with content
5. ✅ Use `:` for typing, `=` for assignment
6. ✅ Check spelling and import paths
7. ✅ Validate files in dependency order

## Reference

For complete validation workflow, see [SKILL.md](../SKILL.md).
For syntax rules, see [SYNTAX.md](SYNTAX.md).
