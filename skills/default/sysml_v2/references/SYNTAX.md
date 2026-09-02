# SysML v2 Syntax Reference

Complete syntax rules for SysML v2 textual notation.

## Table of Contents

- [Import Statements](#import-statements)
- [Package Declarations](#package-declarations)
- [Type Definitions](#type-definitions)
- [Specialization and Typing](#specialization-and-typing)
- [Multiplicity](#multiplicity)
- [Documentation](#documentation)
- [Composition vs Reference](#composition-vs-reference)
- [Value Assignment](#value-assignment)
- [Connections](#connections)
- [Constraints](#constraints)
- [Enumerations (Not Supported)](#enumerations-not-supported)
- [Naming Conventions](#naming-conventions)
- [Standard Library Types](#standard-library-types)
- [Complete Example](#complete-example)
- [Reference](#reference)

## Import Statements

**CRITICAL: Visibility modifier is required**

```sysml
// Wildcard import (all public members)
private import PackageName::*;

// Specific type import
private import PackageName::TypeName;

// Nested package import
public import PackageName::SubPackage::*;

// Multiple imports
private import ScalarValues::*;
private import Base::DataValue;
public import IBMCloud::ResourceLibrary::*;
```

**Visibility modifiers**:
- `private` - Imported members not re-exported (most common)
- `public` - Imported members re-exported to importers of this package

## Package Declarations

```sysml
// Regular package
package MyPackage {
    private import ScalarValues::*;
    // contents
}

// Nested package with qualified name
package Parent::Child {
    // contents
}

// Standard library package
standard library package LibraryName {
    // library contents
}
```

## Type Definitions

### Attribute Definitions (Data Values)

**Must specialize Base::DataValue or subtypes** (String, Real, Integer, Boolean)

```sysml
attribute def Region :> String;
attribute def CPU :> Real;
attribute def InstanceCount :> Integer;
attribute def IsEnabled :> Boolean;
attribute def ServicePlan :> String;
```

### Part Definitions (Structural Elements)

**Implicitly specializes Parts::Part**

```sysml
part def CodeEngineProject {
    // Attributes (owned properties)
    attribute name : String;
    attribute region : Region;
    attribute resourceGroup : String;

    // Nested parts (composition)
    part applications : CodeEngineApp[0..*];
    part secrets : Secret[0..*];
}

part def CodeEngineApp {
    attribute cpu : Real;
    attribute memory : String;
    attribute minScale : Integer;
    attribute maxScale : Integer;

    // Reference (not composition)
    ref containerImage : ContainerImage[0..1];
}
```

### Requirement Definitions

```sysml
requirement def PerformanceRequirement {
    doc /* Performance constraints for the system */

    subject system : System;
    require constraint : cpu < 2.0;
}

requirement def ScalabilityRequirement {
    subject app : Application;
    require constraint : maxScale >= 10;
}
```

## Specialization and Typing

### Specialization (`:>`)

Used for inheritance/subtyping:

```sysml
// Attribute definitions
attribute def MyRegion :> String;
attribute def MyNumber :> Real;

// Part definitions
part def MyComponent :> BaseComponent;

// Explicit part specialization
part def WebServer :> Server {
    attribute protocol : String;
}
```

### Typing (`:`)

Used for typing instances:

```sysml
// Attribute instances
attribute name : String;
attribute cpu : Real;
attribute count : Integer;

// Part instances
part myApp : CodeEngineApp;
part database : PostgreSQL;
```

### Redefinition (`:>>`)

Used for subset/redefinition:

```sysml
end source: Anything :>> BinaryConnection::source;
end target: Anything :>> BinaryConnection::target;
```

## Multiplicity

```sysml
part items : Item[0..*];        // Zero or more (unbounded)
part primary : Item[1..1];       // Exactly one (default)
part optional : Item[0..1];      // Zero or one (optional)
part several : Item[2..5];       // Between 2 and 5
part atLeastTwo : Item[2..*];    // Two or more

// Default multiplicity is [1..1] if not specified
part single : Item;              // Equivalent to Item[1..1]
```

## Documentation

```sysml
// Single-line documentation
doc /* This is a single-line documentation comment */

// Multi-line documentation
doc
/*
 * This is a multi-line documentation comment.
 * It appears in generated documentation.
 * Use for detailed descriptions.
 */
```

## Composition vs Reference

### Composition (`part`)

Owner contains and controls lifetime:

```sysml
part def Application {
    // Composition - app owns these secrets
    part secrets : Secret[0..*];

    // Composition - app owns these environment variables
    part envVars : EnvironmentVariable[0..*];
}
```

### Reference (`ref`)

Points to something owned elsewhere:

```sysml
part def Application {
    // Reference - app uses external image
    ref containerImage : ContainerImage[0..1];

    // Reference - app uses shared database
    ref database : Database[0..1];
}
```

## Value Assignment

```sysml
part myApp : Application {
    // Literal value assignment
    attribute name = "my-application";
    attribute cpu = 2.0;
    attribute memory = "4G";
    attribute enabled = true;

    // Reference assignment
    ref containerImage = sharedRegistry.appImage;
}
```

## Connections

```sysml
part deployment {
    part brokerApp : Application;
    part database : Database;

    // Explicit connection
    connect brokerApp to database;
}

// With typed connection
connection def ServiceConnection {
    end client : Application[1..*];
    end service : Service[1..1];
}

part system {
    part app : Application;
    part api : Service;

    connection appToApi : ServiceConnection {
        end client = app;
        end service = api;
    }
}
```

## Constraints

```sysml
part def Server {
    attribute cpu : Real;
    attribute memory : Real;

    // Inline constraint
    assert constraint cpuMemoryRatio {
        memory / cpu >= 2.0 and memory / cpu <= 8.0
    }
}
```

## Enumerations (Not Supported)

**SysML v2 does not support enum keyword**

```sysml
// ❌ WRONG - No enum support
enum def Region {
    US_EAST, US_SOUTH
}

// ✅ CORRECT - Use attribute def with String
attribute def Region :> String;

// Then use string literals in instances
part myResource {
    attribute region = "us-east";
}
```

## Naming Conventions

**Recommended conventions**:

- **Packages**: `CamelCase` (e.g., `IBMCloud`, `ResourceLibrary`)
- **Type definitions**: `CamelCase` (e.g., `CodeEngineProject`, `Region`)
- **Instances**: `camelCase` (e.g., `decisymProject`, `primaryRegion`)
- **Attributes**: `camelCase` (e.g., `resourceGroup`, `minScale`)

## Standard Library Types

**From ScalarValues**:
- `String` - Text values
- `Real` - Floating-point numbers
- `Integer` - Whole numbers
- `Boolean` - true/false values

**Common imports**:

```sysml
private import ScalarValues::*;        // String, Real, Integer, Boolean
private import Base::DataValue;        // Base type for attribute defs
private import Parts::Part;            // Base type for part defs (implicit)
```

## Complete Example

```sysml
package IBMCloud {
    private import ScalarValues::*;

    // Value types
    attribute def Region :> String;
    attribute def CPU :> Real;
    attribute def Memory :> String;

    // Resource types
    part def CodeEngineApp {
        attribute name : String;
        attribute cpu : CPU;
        attribute memory : Memory;
        attribute minScale : Integer;
        attribute maxScale : Integer;

        ref containerImage : ContainerImage[0..1];

        assert constraint cpuRange {
            cpu >= 0.125 and cpu <= 8.0
        }

        assert constraint scaleRange {
            minScale >= 0 and maxScale <= 250
        }
    }

    // Deployment instance
    part production {
        part brokerApp : CodeEngineApp {
            attribute name = "decisym-broker";
            attribute cpu = 0.125;
            attribute memory = "250M";
            attribute minScale = 0;
            attribute maxScale = 10;
        }
    }
}
```

## Reference

For complete language specification, see: https://www.omg.org/spec/SysML/2.0/
