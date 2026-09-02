# SysML v2 Common Patterns and Examples

Reusable patterns for modeling infrastructure and systems.

## Table of Contents

- [Pattern: Cloud Resource Modeling](#pattern-cloud-resource-modeling)
- [Pattern: Composition vs Reference](#pattern-composition-vs-reference)
- [Pattern: Connections and Dependencies](#pattern-connections-and-dependencies)
- [Pattern: Constraints and Requirements](#pattern-constraints-and-requirements)
- [Pattern: Namespaces and Visibility](#pattern-namespaces-and-visibility)
- [Pattern: Reusable Component Definitions](#pattern-reusable-component-definitions)
- [Pattern: IBM Cloud Code Engine Model](#pattern-ibm-cloud-code-engine-model)
- [Reference](#reference)

## Pattern: Cloud Resource Modeling

Three-layer architecture for cloud infrastructure models.

### Layer 1: Value Types

Define common data types and enumerations.

```sysml
package Cloud {
    private import ScalarValues::*;

    // Simple string-based types
    attribute def Region :> String;
    attribute def ResourceGroup :> String;
    attribute def ServicePlan :> String;

    // Numeric types
    attribute def CPU :> Real;
    attribute def MemoryGB :> Real;
    attribute def StorageGB :> Real;

    // Boolean types
    attribute def IsEnabled :> Boolean;
}
```

### Layer 2: Resource Library

Define reusable resource type definitions.

```sysml
package Cloud::Resources {
    private import ScalarValues::*;
    private import Cloud::*;

    part def ComputeInstance {
        attribute name : String;
        attribute region : Region;
        attribute cpu : CPU;
        attribute memory : String;
        attribute isEnabled : IsEnabled;

        assert constraint cpuRange {
            cpu >= 0.125 and cpu <= 8.0
        }
    }

    part def StorageBucket {
        attribute name : String;
        attribute region : Region;
        attribute storageClass : String;
        attribute size : StorageGB;
    }

    part def DatabaseInstance {
        attribute name : String;
        attribute region : Region;
        attribute engine : String;
        attribute version : String;
    }
}
```

### Layer 3: Deployment Instances

Define concrete instances with actual values.

```sysml
package Cloud::Deployment {
    private import ScalarValues::*;
    private import Cloud::*;
    private import Cloud::Resources::*;

    part production {
        part webServer : ComputeInstance {
            attribute name = "web-01";
            attribute region = "us-east";
            attribute cpu = 2.0;
            attribute memory = "4G";
            attribute isEnabled = true;
        }

        part dataBucket : StorageBucket {
            attribute name = "production-data";
            attribute region = "us-east";
            attribute storageClass = "standard";
            attribute size = 100.0;
        }

        part database : DatabaseInstance {
            attribute name = "prod-db";
            attribute region = "us-east";
            attribute engine = "postgresql";
            attribute version = "15.0";
        }
    }
}
```

## Pattern: Composition vs Reference

### Composition (Owned Parts)

Use when the owner controls the lifetime of the contained parts.

```sysml
part def Application {
    // Composition - app owns these secrets
    part secrets : Secret[0..*];

    // Composition - app owns these environment variables
    part envVars : EnvironmentVariable[0..*];

    // Composition - app owns its configuration
    part config : Configuration[1..1];
}

part def Secret {
    attribute name : String;
    attribute value : String;
}

part def EnvironmentVariable {
    attribute key : String;
    attribute value : String;
}

part def Configuration {
    attribute timeout : Integer;
    attribute retries : Integer;
}
```

### Reference (External Resources)

Use when referencing something owned elsewhere.

```sysml
part def Application {
    // Reference - app uses external image (doesn't own it)
    ref containerImage : ContainerImage[0..1];

    // Reference - app uses shared database (doesn't own it)
    ref database : Database[0..1];

    // Reference - app uses shared secret store
    ref secretStore : SecretStore[0..1];
}

// Deployment with references
part deployment {
    // Shared resources
    part sharedDatabase : Database {
        attribute name = "shared-db";
    }

    part registry : ContainerRegistry {
        part appImage : ContainerImage {
            attribute name = "my-app:latest";
        }
    }

    // Application references shared resources
    part app1 : Application {
        ref containerImage = registry.appImage;
        ref database = sharedDatabase;
    }

    part app2 : Application {
        ref containerImage = registry.appImage;
        ref database = sharedDatabase;  // Same database
    }
}
```

## Pattern: Connections and Dependencies

### Simple Connections

```sysml
part deployment {
    part brokerApp : Application;
    part database : Database;
    part cacheService : Cache;

    // Explicit connections
    connect brokerApp to database;
    connect brokerApp to cacheService;
}
```

### Typed Connections

```sysml
connection def ServiceConnection {
    end client : Application[1..*];
    end service : Service[1..1];
}

connection def DataConnection {
    end consumer : Application[1..*];
    end provider : Database[1..1];
}

part system {
    part webapp : Application;
    part apiService : Service;
    part datastore : Database;

    connection webToApi : ServiceConnection {
        end client = webapp;
        end service = apiService;
    }

    connection apiToData : DataConnection {
        end consumer = apiService;
        end provider = datastore;
    }
}
```

## Pattern: Constraints and Requirements

### Attribute Constraints

```sysml
part def Server {
    attribute cpu : Real;
    attribute memory : Real;

    // CPU-to-memory ratio constraint
    assert constraint cpuMemoryRatio {
        memory / cpu >= 2.0 and memory / cpu <= 8.0
    }

    // CPU range constraint
    assert constraint cpuRange {
        cpu >= 0.125 and cpu <= 16.0
    }
}
```

### Requirements

```sysml
requirement def PerformanceRequirement {
    doc /* System must meet performance targets */

    subject system : System;

    require constraint responseTime {
        system.responseTimeMs < 200.0
    }
}

requirement def ScalabilityRequirement {
    doc /* System must scale to handle load */

    subject app : Application;

    require constraint {
        app.maxScale >= 10 and app.minScale >= 0
    }
}

// Apply requirements to deployment
part production {
    part webApp : Application {
        attribute maxScale = 100;
        attribute minScale = 2;
    }

    satisfy scalabilityReq : ScalabilityRequirement {
        subject app = webApp;
    }
}
```

## Pattern: Namespaces and Visibility

### Organizing with Packages

```sysml
// Base value types package
package IBMCloud {
    private import ScalarValues::*;

    attribute def Region :> String;
    attribute def ResourceGroup :> String;
}

// Nested package for specific service
package IBMCloud::CodeEngine {
    private import ScalarValues::*;
    private import IBMCloud::*;

    part def Project {
        attribute name : String;
        attribute region : Region;
        attribute resourceGroup : ResourceGroup;
    }

    part def App {
        attribute name : String;
        attribute cpu : Real;
        attribute memory : String;
    }
}

// Another service in parallel namespace
package IBMCloud::ContainerRegistry {
    private import ScalarValues::*;
    private import IBMCloud::*;

    part def Namespace {
        attribute name : String;
        attribute region : Region;
    }

    part def Image {
        attribute name : String;
        attribute tag : String;
    }
}
```

## Pattern: Reusable Component Definitions

### Base Component with Variants

```sysml
package Infrastructure {
    private import ScalarValues::*;

    // Base compute resource
    part def ComputeResource {
        attribute name : String;
        attribute cpu : Real;
        attribute memory : String;

        assert constraint {
            cpu > 0.0 and memory != ""
        }
    }

    // Specialized variants
    part def Container :> ComputeResource {
        attribute image : String;
        attribute ports : Integer[0..*];
    }

    part def VirtualMachine :> ComputeResource {
        attribute diskSize : Real;
        attribute osType : String;
    }

    part def ServerlessFunction :> ComputeResource {
        attribute runtime : String;
        attribute timeout : Integer;
    }
}

// Usage in deployment
package Deployment {
    private import ScalarValues::*;
    private import Infrastructure::*;

    part system {
        part webapp : Container {
            attribute name = "web-app";
            attribute cpu = 1.0;
            attribute memory = "2G";
            attribute image = "nginx:latest";
        }

        part worker : ServerlessFunction {
            attribute name = "background-worker";
            attribute cpu = 0.5;
            attribute memory = "512M";
            attribute runtime = "python3.9";
            attribute timeout = 300;
        }
    }
}
```

## Pattern: IBM Cloud Code Engine Model

Complete example modeling IBM Cloud Code Engine.

```sysml
package IBMCloud::CodeEngine {
    private import ScalarValues::*;

    // Value types
    attribute def Region :> String;
    attribute def CPU :> Real;
    attribute def Memory :> String;

    // Resource definitions
    part def Project {
        attribute name : String;
        attribute region : Region;
        attribute resourceGroup : String;

        // Project contains apps and secrets
        part apps : App[0..*];
        part secrets : Secret[0..*];
    }

    part def App {
        attribute name : String;
        attribute cpu : CPU;
        attribute memory : Memory;
        attribute minScale : Integer;
        attribute maxScale : Integer;

        // App references external image
        ref containerImage : ContainerImage[0..1];

        // App owns environment variables
        part envVars : EnvVar[0..*];

        // Constraints
        assert constraint cpuRange {
            cpu >= 0.125 and cpu <= 8.0
        }

        assert constraint scaleRange {
            minScale >= 0 and maxScale <= 250
        }
    }

    part def Secret {
        attribute name : String;
        attribute format : String;
    }

    part def EnvVar {
        attribute key : String;
        attribute value : String;
    }

    part def ContainerImage {
        attribute url : String;
        attribute tag : String;
    }
}

// Deployment instance
package DeciSym::Deployment {
    private import ScalarValues::*;
    private import IBMCloud::CodeEngine::*;

    part production : Project {
        attribute name = "decisym-osb-broker";
        attribute region = "us-east";
        attribute resourceGroup = "decisym-wxo-agents";

        part brokerApp : App {
            attribute name = "decisym-broker";
            attribute cpu = 0.125;
            attribute memory = "250M";
            attribute minScale = 0;
            attribute maxScale = 10;

            part apiKeyEnv : EnvVar {
                attribute key = "WATSONX_API_KEY";
                attribute value = "${SECRET}";
            }
        }

        part registrySecret : Secret {
            attribute name = "icr-pull-secret";
            attribute format = "registry";
        }
    }
}
```

## Reference

For syntax details, see [SYNTAX.md](SYNTAX.md).
For validation workflow, see [SKILL.md](../SKILL.md).
