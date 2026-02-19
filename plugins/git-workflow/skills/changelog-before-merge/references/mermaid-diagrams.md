# Mermaid Diagrams Requirements

## Diagram Count by PR Size

| PR Size | Minimum Diagrams |
|---------|------------------|
| 1-10 files | 1 (architecture) |
| 11-30 files | 3 (arch + flow + tests) |
| 31+ files | 5 (full set) |

---

## Required Diagram Types

### 1. Component Architecture (REQUIRED)

Shows module relationships and layers.

```mermaid
flowchart TB
    subgraph Layer1["UI Layer"]
        A[Component A]
    end
    subgraph Layer2["Business Logic"]
        B[Service B]
    end
    A --> B
```

### 2. Data/Request Flow (REQUIRED for features)

Sequence diagram showing request flow.

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    participant D as Database
    C->>S: Request
    S->>D: Query
    D-->>S: Result
    S-->>C: Response
```

### 3. State Changes (REQUIRED for refactors)

Before/After comparison.

```mermaid
flowchart LR
    subgraph Before["Before Refactor"]
        OA[Old Architecture]
    end
    subgraph After["After Refactor"]
        NA[New Architecture]
    end
    OA -.->|refactor| NA
```

### 4. Test Coverage Map (REQUIRED)

Test file hierarchy.

```mermaid
graph TB
    subgraph Tests
        E2E[E2E Tests]
        INT[Integration]
        UNIT[Unit Tests]
    end
    E2E --> INT --> UNIT
```

### 5. Dependency Graph (if new deps added)

New dependencies visualization.

```mermaid
flowchart TD
    APP[Application]
    APP --> DEP1[New Dependency]
    APP --> DEP2[Existing Dep]
```

---

## Depth-Level Requirements

### Standard Depth
- 1-3 diagrams
- Focus on architecture and data flow
- Basic test coverage map if tests added

### Thorough Depth (`--depth thorough`)
- 5+ diagrams
- All diagram types
- Detailed before/after for refactors
- Full dependency graph
- Security-relevant flows if sensitive files

---

## Mermaid Syntax Tips

**Subgraphs for grouping:**
```mermaid
subgraph GroupName["Display Label"]
    Node1[Content]
    Node2[Content]
end
```

**Arrow types:**
- `-->` solid arrow
- `-.->` dotted arrow
- `==>` thick arrow
- `-->>` async/message arrow

**Node shapes:**
- `[Text]` rectangle
- `(Text)` rounded
- `{Text}` diamond
- `[(Text)]` cylinder (database)

**Styling:**
```mermaid
style NodeId fill:#f9f,stroke:#333
```
