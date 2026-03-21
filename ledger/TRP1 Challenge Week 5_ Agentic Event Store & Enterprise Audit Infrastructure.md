# TRP1 WEEK 5: The Ledger

## *Agentic Event Store & Enterprise Audit Infrastructure.*  *Building the immutable memory and governance backbone for multi-agent AI systems at production scale.*

## ***If 2025 was the year of the agent, 2026 is the year multi-agent systems move into production. The shift depends on one thing: infrastructure that can be trusted. An event store is not optional infrastructure for production AI. It is the foundation.***

**Builds on:**  
**Week 1** Governance Hooks & Intent Traceability     
**Week 2** Automaton Auditor     
**Week 4** Brownfield Cartographer

# **Why This Project**

Every system you have built in this program has a memory problem. The Cartographer's lineage graph is rebuilt from scratch on each run. The Automaton Auditor's judgements are lost when the process ends. Week 1's governance hooks produce an intent log that no other system reads. These are not bugs — they are the natural limitations of systems that have no shared, persistent, append-only memory.

The Ledger fixes this permanently. It is the event store that all other systems in this program should have been writing to from Week 1\. By the end of this week, you will have a production-quality event sourcing infrastructure that: makes agent decisions auditable and reproducible, enables temporal queries for compliance and debugging, provides the append-only ledger that prevents the ephemeral memory failure mode described in the Gas Town pattern, and exposes everything as a typed, queryable API that downstream systems can consume.

The business case is precise. In 2026, the number-one reason enterprise AI deployments fail to reach production is not model quality — it is governance and auditability. Regulators, auditors, and enterprise risk teams require an immutable record of every AI decision and the data that informed it. The Ledger is that record. An FDE who can deploy it in the first week of a client engagement immediately unblocks the governance conversation that is otherwise the last thing to get resolved.

## **The Compounding Connection**

This project is the retroactive foundation for the entire program. When you build the Ledger, you are building the infrastructure that all prior projects should have been using. Your Week 2 audit verdicts become events in a GovernanceJudgement stream. The Ledger does not add a new system — it connects the ones you already have.

# **New Skills This Week**

## **Technical Skills**

* **Event store schema design:** Append-only tables, stream partitioning, hot/cold storage, PostgreSQL LISTEN/NOTIFY for real-time subscriptions  
* **CQRS implementation:** Separating command handlers from query handlers, projection management, eventual consistency patterns  
* **Aggregate design:** Consistency boundaries, business rule enforcement in domain logic, state machine patterns  
* **Optimistic concurrency control:** Version-based conflict detection, retry strategies, conflict resolution patterns  
* **Async projection daemon:** Checkpoint management, fault-tolerant background processing, projection lag measurement  
* **Upcasting & schema evolution:** Version migration chains, inference vs. null strategies, immutability guarantees  
* **Cryptographic audit chains:** Hash chain construction, tamper detection, regulatory package generation  
* **MCP command/resource architecture:** Tool design for LLM consumers, structured error types, precondition documentation

## **FDE Skills**

* **The governance conversation:** Ability to translate "we need auditability" from a risk/compliance stakeholder into a specific event store deployment recommendation within 48 hours  
* **Enterprise stack translation:** Mapping your PostgreSQL implementation to Marten/Wolverine (.NET) and EventStoreDB for clients who already have a stack preference  
* **The one-way door conversation:** Knowing how to communicate the migration complexity and long-term commitment of adopting event sourcing, so clients make the decision with accurate information  
* **SLO-based architecture:** Designing systems to explicit performance contracts rather than "as fast as possible" — the foundation of production-grade FDE work

# **The Week Standard**

By end of this week, you must be able to demonstrate: "Show me the complete decision history of application ID X" — from first event to final decision, with every AI agent action, every compliance check, every human review, all causal links intact, temporal query to any point in the lifecycle, and cryptographic integrity verification. If you cannot run this demonstration in under 60 seconds, the week is not complete.

# **Reading Material**

* [An empirical characterization of event sourced systems and their schema evolution — Lessons from industry](https://www.sciencedirect.com/science/article/pii/S0164121221000674) 

# **![][image1]**

# **Phase 0 — Domain Reconnaissance (Day 1, Morning)**

Event sourcing is one of the most misunderstood patterns in enterprise software. Most engineers who say they have used it have used a version of it — usually without optimistic concurrency control, without projection management, without upcasting, and without understanding why any of those things matter. Phase 0 establishes the conceptual precision required to build the Ledger correctly.

## **Core Concepts — Required Mastery**

**Event Sourcing vs. Event-Driven Architecture**

*These are not the same thing. Event-Driven Architecture (EDA) uses events as messages between services — the sender fires and forgets. Event Sourcing uses events as the system's source of truth — the events ARE the database. Your system today (agent activity tracing component callbacks, the Automaton Auditor's verdict stream) is EDA. The Ledger is event sourcing. The distinction matters because EDA events can be dropped or lost; event store entries never can. Study: Confluent's "Future of AI Agents is Event-Driven" (2025) and contrast with Greg Young's "CQRS and Event Sourcing" talks.*

**Aggregate Boundaries & Domain Events**

*An aggregate is a consistency boundary — a cluster of domain objects that must be mutated atomically. An event is the record of a fact that happened to an aggregate. The critical rule: aggregates communicate only through events, never through direct method calls. In the AI era, each AI agent is a natural aggregate boundary: its decisions are facts, recorded as events, never mutated. Study: Vernon's "Implementing Domain-Driven Design", Chapter 10 on aggregates.*

**CQRS — Command Query Responsibility Segregation**

*Write operations (Commands) and read operations (Queries) are handled by separate models. Commands append events to streams. Queries read from projections built from those events. The separation enables: independent scaling of reads and writes, multiple read-optimised projections from the same event stream, and the ability to rebuild any read model by replaying events. In the MCP context: MCP Tools are Commands; MCP Resources are Queries against projections.*

**Optimistic Concurrency Control**

*In an event store, two processes can simultaneously try to append to the same stream. Without concurrency control, you get split-brain state. The solution: every append operation specifies an expected\_version — the stream version it read before making its decision. If the stream's actual version has advanced (because someone else appended), the operation is rejected with a concurrency exception. The caller must reload and retry. This is how the Ledger prevents two AI agents from simultaneously making conflicting decisions. No locks required. No transactions spanning multiple aggregates.*

**Projections — Inline vs. Async**

*A projection transforms events into a read model. Inline projections update synchronously in the same transaction as the event write — strong consistency, higher write latency. Async projections update asynchronously via a background daemon — lower write latency, eventual consistency, and the ability to rebuild from scratch by replaying. The Marten library (the enterprise .NET standard for PostgreSQL-backed event stores) calls its async projection runner the "Async Daemon." Python equivalents achieve the same pattern with background asyncio tasks. Study: Marten docs on projection lifecycle; EventStoreDB catch-up subscriptions.*

**Upcasting — Handling Schema Evolution**

*In a CRUD system, you run a migration and the data changes. In an event store, the past is immutable. When your event schema evolves, you write an upcaster — a function that transforms old event structures into new ones at read time, without touching the stored events. This is the event sourcing solution to the problem identified by schema evolution analysis tools. In production, upcasters are registered in a chain: v1→v2→v3, applied automatically whenever old events are loaded. An event store without upcasting is an event store that will eventually break under the weight of its own history.*

**The Outbox Pattern — Guaranteed Event Delivery**

*The classic distributed systems problem: you append an event to the store AND need to publish it to a message bus (Kafka, Redis Streams, RabbitMQ). If the store write succeeds but the publish fails, your read models and downstream systems are inconsistent. The Outbox Pattern solves this: write events to both the event store and an "outbox" table in the same database transaction. A separate process polls the outbox and publishes reliably. This is how you connect The Ledger to the Polyglot Bridge (Week 10).*

**The Gas Town Persistent Ledger Pattern**

*Named for the infrastructure pattern in agentic systems where agent context is lost on process restart. The solution: every agent action is written to the event store as an event before the action is executed. On restart, the agent replays its event stream to reconstruct its context window. This is not just logging — it is the agent's memory backed by the most reliable storage primitive available: an append-only, ACID-compliant, PostgreSQL-backed event stream.*

## 

## **Stack Orientation — Enterprise Tools in 2026**

The enterprise market has converged on two primary event store backends. You must understand both even if you implement only one.

| TOOL | STACK | BEST FOR | ENTERPRISE ADOPTION | YOUR CHOICE IN THIS CHALLENGE |
| :---- | :---- | :---- | :---- | :---- |
| PostgreSQL \+ psycopg3 | Python (primary) | Single-database architectures; teams already on Postgres; FDE rapid deployment | Extremely high — Postgres is everywhere | PRIMARY — Build the event store schema and all phases using Postgres \+ asyncpg/psycopg3 |
| EventStoreDB 24.x | Any (HTTP API) | Dedicated high-throughput event stores; persistent subscriptions at scale; native gRPC streaming | Growing — the purpose-built standard | REFERENCE — Know the API; document in DOMAIN\_NOTES how your Postgres schema maps to EventStoreDB concepts |
| Marten 7.x \+ Wolverine | .NET / C\# | Enterprise .NET shops; Async Daemon for projection management; Wolverine for command routing | Dominant in .NET enterprise | CONCEPTUAL — Study the architecture; your Python implementation should mirror the same patterns |
| Kafka \+ Kafka Streams | Any | Very-high-throughput event streaming; not a true event store (retention limits) | Ubiquitous in large enterprise | INTEGRATION — Week 10 connects The Ledger to Kafka via the Outbox pattern |
| Redis Streams | Any | Lower-latency pub/sub; projection fan-out; not durable by default | Common as event bus layer | INTEGRATION — Use Redis Streams for real-time projection update notifications |

## **DOMAIN\_NOTES.md — Graded Deliverable**

Produce a DOMAIN\_NOTES.md before writing any implementation code. It must answer all of the following with specificity, not generality. This document is assessed independently of your code — a candidate who writes excellent code but cannot reason about the tradeoffs is not ready for enterprise event sourcing work.

1. **EDA vs. ES distinction:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this Event-Driven Architecture (EDA) or Event Sourcing (ES)? If you redesigned it using The Ledger, what exactly would change in the architecture and what would you gain?  
2. **The aggregate question:** In the scenario below, you will build four aggregates. Identify one alternative boundary you considered and rejected. What coupling problem does your chosen boundary prevent?  
3. **Concurrency in practice:** Two AI agents simultaneously process the same loan application and both call append\_events with expected\_version=3. Trace the exact sequence of operations in your event store. What does the losing agent receive, and what must it do next?  
4. **Projection lag and its consequences:** Your LoanApplication projection is eventually consistent with a typical lag of 200ms. A loan officer queries "available credit limit" immediately after an agent commits a disbursement event. They see the old limit. What does your system do, and how do you communicate this to the user interface?  
5. **The upcasting scenario:** The CreditDecisionMade event was defined in 2024 with {application\_id, decision, reason}. In 2026 it needs {application\_id, decision, reason, model\_version, confidence\_score, regulatory\_basis}. Write the upcaster. What is your inference strategy for historical events that predate model\_version?  
6. **The Marten Async Daemon parallel:** Marten 7.0 introduced distributed projection execution across multiple nodes. Describe how you would achieve the same pattern in your Python implementation. What coordination primitive do you use, and what failure mode does it guard against?

## 

# **The Scenario — Apex Financial Services**

Apex Financial Services is deploying a multi-agent AI platform to process commercial loan applications. Four specialized AI agents collaborate on each application: a CreditAnalysis agent evaluates financial risk, a FraudDetection agent screens for anomalous patterns, a ComplianceAgent verifies regulatory eligibility, and a DecisionOrchestrator synthesises their outputs and produces a final recommendation. Human loan officers review the recommendation and make the final binding decision.

The regulatory environment requires: a complete, immutable audit trail of every AI decision and the data that informed it; the ability to reconstruct the exact state of any application at any point in time for regulatory examination; temporal queries (e.g., "what would the credit decision have been if we had used last month's risk model?"); and cryptographic integrity — any tampering with the audit trail must be detectable. The CTO has mandated that the system must not be modified to add auditability after the fact — auditability must be the architecture, not an annotation.

This is the canonical environment where event sourcing is not just beneficial — it is the only architecture that satisfies the requirements. Your task is to build The Ledger: the event store and its surrounding infrastructure that makes this system governable.

## **Why This Scenario**

Financial services is the highest-density event sourcing environment in enterprise software. Every loan decision, every risk calculation, every compliance check is a regulated event. The same architecture applies directly to any domain where audit trails are non-negotiable: healthcare prior authorisations, government benefit decisions, insurance claim adjudication, and — directly relevant to your work — AI agent decision logs in any enterprise deployment. Master this scenario and you have mastered the pattern for all of them.

## **The Four Aggregates**

| AGGREGATE | STREAM ID FORMAT | WHAT IT TRACKS | KEY BUSINESS INVARIANTS |
| :---- | :---- | :---- | :---- |
| LoanApplication | loan-{application\_id} | Full lifecycle of a commercial loan application from submission to decision | Cannot transition from Approved to UnderReview; cannot be approved if compliance check is pending; credit limit cannot exceed agent-assessed maximum |
| AgentSession | agent-{agent\_id}-{session\_id} | All actions taken by a specific AI agent instance during a work session, including model version, input data hashes, reasoning trace, and outputs | Every output event must reference a ContextLoaded event; every decision must reference the specific model version that produced it |
| ComplianceRecord | compliance-{application\_id} | Regulatory checks, rule evaluations, and compliance verdicts for each application | Cannot issue a compliance clearance without all mandatory checks; every check must reference the specific regulation version evaluated against |
| AuditLedger | audit-{entity\_type}-{entity\_id} | Cross-cutting audit trail linking events across all aggregates for a single business entity | Append-only; no events may be removed; must maintain cross-stream causal ordering via correlation\_id chains |

## **The Event Catalogue**

These are the events you will implement. The catalogue is intentionally incomplete — identifying the missing events is part of the Phase 1 domain exercise.

| EVENT TYPE | AGGREGATE | VERSION | KEY PAYLOAD FIELDS |
| :---- | :---- | :---- | :---- |
| ApplicationSubmitted | LoanApplication | 1 | application\_id, applicant\_id, requested\_amount\_usd, loan\_purpose, submission\_channel, submitted\_at |
| CreditAnalysisRequested | LoanApplication | 1 | application\_id, assigned\_agent\_id, requested\_at, priority |
| CreditAnalysisCompleted | AgentSession | 2 | application\_id, agent\_id, session\_id, model\_version, confidence\_score, risk\_tier, recommended\_limit\_usd, analysis\_duration\_ms, input\_data\_hash |
| FraudScreeningCompleted | AgentSession | 1 | application\_id, agent\_id, fraud\_score, anomaly\_flags\[\], screening\_model\_version, input\_data\_hash |
| ComplianceCheckRequested | ComplianceRecord | 1 | application\_id, regulation\_set\_version, checks\_required\[\] |
| ComplianceRulePassed | ComplianceRecord | 1 | application\_id, rule\_id, rule\_version, evaluation\_timestamp, evidence\_hash |
| ComplianceRuleFailed | ComplianceRecord | 1 | application\_id, rule\_id, rule\_version, failure\_reason, remediation\_required |
| DecisionGenerated | LoanApplication | 2 | application\_id, orchestrator\_agent\_id, recommendation (APPROVE/DECLINE/REFER), confidence\_score, contributing\_agent\_sessions\[\], decision\_basis\_summary, model\_versions{} |
| HumanReviewCompleted | LoanApplication | 1 | application\_id, reviewer\_id, override (bool), final\_decision, override\_reason (if override) |
| ApplicationApproved | LoanApplication | 1 | application\_id, approved\_amount\_usd, interest\_rate, conditions\[\], approved\_by (human\_id or "auto"), effective\_date |
| ApplicationDeclined | LoanApplication | 1 | application\_id, decline\_reasons\[\], declined\_by, adverse\_action\_notice\_required (bool) |
| AgentContextLoaded | AgentSession | 1 | agent\_id, session\_id, context\_source, event\_replay\_from\_position, context\_token\_count, model\_version |
| AuditIntegrityCheckRun | AuditLedger | 1 | entity\_id, check\_timestamp, events\_verified\_count, integrity\_hash, previous\_hash (chain) |

# **PHASE 1  ·  The Event Store Core — PostgreSQL Schema & Interface**

Build the event store foundation. Everything else is built on this. The schema is not a suggestion — it is the contract that every other component in this program will eventually write to and read from. Please identify and report if there are missing elements that could improve the schema validity in future scenarios. 

## **Database Schema**

Create the following tables. Justify every column in DESIGN.md — columns you cannot justify should not exist.

CREATE TABLE events (  
  event\_id         UUID PRIMARY KEY DEFAULT gen\_random\_uuid(),  
  stream\_id        TEXT NOT NULL,  
  stream\_position  BIGINT NOT NULL,  
  global\_position  BIGINT GENERATED ALWAYS AS IDENTITY,  
  event\_type       TEXT NOT NULL,  
  event\_version    SMALLINT NOT NULL DEFAULT 1,  
  payload          JSONB NOT NULL,  
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,  
  recorded\_at      TIMESTAMPTZ NOT NULL DEFAULT clock\_timestamp(),  
  CONSTRAINT uq\_stream\_position UNIQUE (stream\_id, stream\_position)  
);

CREATE INDEX idx\_events\_stream\_id ON events (stream\_id, stream\_position);  
CREATE INDEX idx\_events\_global\_pos ON events (global\_position);  
CREATE INDEX idx\_events\_type ON events (event\_type);  
CREATE INDEX idx\_events\_recorded ON events (recorded\_at);

CREATE TABLE event\_streams (  
  stream\_id        TEXT PRIMARY KEY,  
  aggregate\_type   TEXT NOT NULL,  
  current\_version  BIGINT NOT NULL DEFAULT 0,  
  created\_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),  
  archived\_at      TIMESTAMPTZ,  
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb  
);

CREATE TABLE projection\_checkpoints (  
  projection\_name  TEXT PRIMARY KEY,  
  last\_position    BIGINT NOT NULL DEFAULT 0,  
  updated\_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()  
);

CREATE TABLE outbox (  
  id               UUID PRIMARY KEY DEFAULT gen\_random\_uuid(),  
  event\_id         UUID NOT NULL REFERENCES events(event\_id),  
  destination      TEXT NOT NULL,  
  payload          JSONB NOT NULL,  
  created\_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),  
  published\_at     TIMESTAMPTZ,  
  attempts         SMALLINT NOT NULL DEFAULT 0  
);

## **Core Python Interface**

Implement EventStore as an async Python class. The interface is fixed — implementation is yours.

class EventStore:  
    async def append(  
        self,  
        stream\_id: str,  
        events: list\[BaseEvent\],  
        expected\_version: int,          \# \-1 \= new stream; N \= exact version required  
        correlation\_id: str | None \= None,  
        causation\_id:   str | None \= None,  
    ) \-\> int:                            \# returns new stream version  
        """  
        Atomically appends events to stream\_id.  
        Raises OptimisticConcurrencyError if stream version \!= expected\_version.  
        Writes to outbox in same transaction.  
        """

    async def load\_stream(  
        self,  
        stream\_id: str,  
        from\_position: int \= 0,  
        to\_position:   int | None \= None,  
    ) \-\> list\[StoredEvent\]:             \# events in stream order, upcasted

    async def load\_all(  
        self,  
        from\_global\_position: int \= 0,  
        event\_types: list\[str\] | None \= None,  
        batch\_size: int \= 500,  
    ) \-\> AsyncIterator\[StoredEvent\]:   \# async generator, efficient for replay

    async def stream\_version(self, stream\_id: str) \-\> int:  
    async def archive\_stream(self, stream\_id: str) \-\> None:  
    async def get\_stream\_metadata(self, stream\_id: str) \-\> StreamMetadata:

## **Optimistic Concurrency — The Double-Decision Test**

This is the most critical test in Phase 1\. Two AI agents simultaneously attempt to append a CreditAnalysisCompleted event to the same loan application stream. Both read the stream at version 3 and pass expected\_version=3 to their append call. Exactly one must succeed. The other must receive OptimisticConcurrencyError and retry after reloading the stream.

Implement a test that spawns two concurrent asyncio tasks doing this. The test must assert: (a) total events appended to the stream \= 4 (not 5), (b) the winning task's event has stream\_position=4, (c) the losing task's OptimisticConcurrencyError is raised, not silently swallowed.

## **Why This Matters**

In the Apex loan scenario, this test represents two fraud-detection agents simultaneously flagging the same application. Without optimistic concurrency, both flags are applied and the application's state becomes inconsistent — no one knows which fraud score is authoritative. With it, one agent's decision wins; the other must reload and see whether its analysis is still relevant. This is not an edge case — at 1,000 applications/hour with 4 agents each, concurrency collisions happen constantly.

## **PHASE 2  ·  Domain Logic — Aggregates, Commands & Business Rules**

Implement the domain logic for LoanApplication and AgentSession. The pattern: command received → aggregate state reconstructed by replaying events → business rules validated → new events appended.

## **The Command Handler Pattern**

\# Every command handler follows this exact structure:  
async def handle\_credit\_analysis\_completed(  
    cmd: CreditAnalysisCompletedCommand,  
    store: EventStore,  
) \-\> None:  
    \# 1\. Reconstruct current aggregate state from event history  
    app \= await LoanApplicationAggregate.load(store, cmd.application\_id)  
    agent \= await AgentSessionAggregate.load(store, cmd.agent\_id, cmd.session\_id)

    \# 2\. Validate — all business rules checked BEFORE any state change  
    app.assert\_awaiting\_credit\_analysis()  
    agent.assert\_context\_loaded()                    \# Gas Town pattern  
    agent.assert\_model\_version\_current(cmd.model\_version)

    \# 3\. Determine new events — pure logic, no I/O  
    new\_events \= \[  
        CreditAnalysisCompleted(  
            application\_id \= cmd.application\_id,  
            agent\_id       \= cmd.agent\_id,  
            session\_id     \= cmd.session\_id,  
            model\_version  \= cmd.model\_version,  
            confidence\_score \= cmd.confidence\_score,  
            risk\_tier      \= cmd.risk\_tier,  
            recommended\_limit\_usd \= cmd.recommended\_limit\_usd,  
            analysis\_duration\_ms  \= cmd.duration\_ms,  
            input\_data\_hash \= hash\_inputs(cmd.input\_data),  
        )  
    \]

    \# 4\. Append atomically — optimistic concurrency enforced by store  
    await store.append(  
        stream\_id        \= f"loan-{cmd.application\_id}",  
        events           \= new\_events,  
        expected\_version \= app.version,  
        correlation\_id   \= cmd.correlation\_id,  
        causation\_id     \= cmd.causation\_id,  
    )

## **Business Rules to Enforce**

The following rules must be enforced in the aggregate domain logic, not in the API layer. A rule that is only checked in a request handler is not a business rule — it is a UI validation.

1. **Application state machine:** Valid transitions only: Submitted → AwaitingAnalysis → AnalysisComplete → ComplianceReview → PendingDecision → ApprovedPendingHuman / DeclinedPendingHuman → FinalApproved / FinalDeclined. Any out-of-order transition raises DomainError.  
2. **Agent context requirement (Gas Town):** An AgentSession aggregate MUST have an AgentContextLoaded event as its first event before any decision event can be appended. This enforces the persistent ledger pattern — no agent may make a decision without first declaring its context source.  
3. **Model version locking:** Once a CreditAnalysisCompleted event is appended for an application, no further CreditAnalysisCompleted events may be appended for the same application unless the first was superseded by a HumanReviewOverride. This prevents analysis churn.  
4. **Confidence floor:** A DecisionGenerated event with confidence\_score \< 0.6 must set recommendation \= "REFER" regardless of the orchestrator's analysis. This is a regulatory requirement, enforced in the aggregate.  
5. **Compliance dependency:** An ApplicationApproved event cannot be appended unless all ComplianceRulePassed events for the application's required checks are present in the ComplianceRecord stream. The LoanApplication aggregate must hold a reference to check this.  
6. **Causal chain enforcement:** Every DecisionGenerated event's contributing\_agent\_sessions\[\] list must reference only AgentSession stream IDs that contain a decision event for this application\_id. An orchestrator that references sessions that never processed this application must be rejected.

## **Aggregate State Reconstruction**

Each aggregate must implement a load() classmethod that replays its event stream and applies each event to build current state. The apply pattern must be explicit — one method per event type:

class LoanApplicationAggregate:  
    @classmethod  
    async def load(cls, store: EventStore, application\_id: str) \-\> "LoanApplicationAggregate":  
        events \= await store.load\_stream(f"loan-{application\_id}")  
        agg \= cls(application\_id=application\_id)  
        for event in events:  
            agg.\_apply(event)  
        return agg

    def \_apply(self, event: StoredEvent) \-\> None:  
        handler \= getattr(self, f"\_on\_{event.event\_type}", None)  
        if handler:  
            handler(event)  
        self.version \= event.stream\_position

    def \_on\_ApplicationSubmitted(self, event: StoredEvent) \-\> None:  
        self.state \= ApplicationState.SUBMITTED  
        self.applicant\_id \= event.payload\["applicant\_id"\]  
        self.requested\_amount \= event.payload\["requested\_amount\_usd"\]

    def \_on\_ApplicationApproved(self, event: StoredEvent) \-\> None:  
        self.state \= ApplicationState.FINAL\_APPROVED  
        self.approved\_amount \= event.payload\["approved\_amount\_usd"\]

# 

# **PHASE 3  ·  Projections — CQRS Read Models & Async Daemon** 

Projections are the read side of CQRS. They subscribe to the event stream and maintain read-optimised views that can be queried without loading and replaying aggregate streams. Build three projections and the async daemon that keeps them current.

## **The Async Projection Daemon**

The daemon is a background asyncio task that continuously polls the events table from the last processed global\_position, processes new events through registered projections, and updates projection\_checkpoints. It must be fault-tolerant: if a projection handler fails, the daemon must log the error, skip the offending event (with configurable retry count), and continue. A daemon that crashes on a bad event is a production incident.

class ProjectionDaemon:  
    def \_\_init\_\_(self, store: EventStore, projections: list\[Projection\]):  
        self.\_store \= store  
        self.\_projections \= {p.name: p for p in projections}  
        self.\_running \= False

    async def run\_forever(self, poll\_interval\_ms: int \= 100\) \-\> None:  
        self.\_running \= True  
        while self.\_running:  
            await self.\_process\_batch()  
            await asyncio.sleep(poll\_interval\_ms / 1000\)

    async def \_process\_batch(self) \-\> None:  
        \# Load lowest checkpoint across all projections  
        \# Load events from that position in batches  
        \# For each event, route to subscribed projections  
        \# Update checkpoints after each successful batch  
        \# Expose lag metric: global\_position \- last\_processed\_position  
        ...

## **Required Projections**

### **Projection 1: ApplicationSummary**

A read-optimised view of every loan application's current state. Stored as a Postgres table (one row per application). Updated inline by the daemon as new events arrive.

**Table schema:**

application\_id, state, applicant\_id,  
requested\_amount\_usd, approved\_amount\_usd,  
risk\_tier, fraud\_score,  
compliance\_status, decision,  
agent\_sessions\_completed\[\],  
last\_event\_type, last\_event\_at,  
human\_reviewer\_id, final\_decision\_at

### **Projection 2: AgentPerformanceLedger**

Aggregated performance metrics per AI agent model version. Enables the question: "Has agent v2.3 been making systematically different decisions than v2.2?"

**Table schema:**

agent\_id, model\_version,  
analyses\_completed, decisions\_generated,  
avg\_confidence\_score, avg\_duration\_ms,  
approve\_rate, decline\_rate, refer\_rate,  
human\_override\_rate,  
first\_seen\_at, last\_seen\_at

### **Projection 3: ComplianceAuditView (Critical)**

This projection is the regulatory read model — the view that a compliance officer or regulator queries when examining an application. It must be complete (every compliance event), traceable (every rule references its regulation version), and temporally queryable (state at any past timestamp).

Unlike the other projections, the ComplianceAuditView must support the temporal query interface: get\_state\_at(application\_id, timestamp) → ComplianceAuditView. This requires a snapshot strategy you must implement and justify in DESIGN.md.

* **get\_current\_compliance(application\_id)** → full compliance record with all checks, verdicts, and regulation versions  
* **get\_compliance\_at(application\_id, timestamp)** → compliance state as it existed at a specific moment (regulatory time-travel)  
* **get\_projection\_lag()** → milliseconds between latest event in store and latest event this projection has processed — must be exposed as a metric  
* **rebuild\_from\_scratch()** → truncate projection table and replay all events from position 0 — must complete without downtime to live reads

## **Projection Lag — The Non-Negotiable Metric**

### **The Lag Contract**

Your ApplicationSummary projection must maintain a lag of under 500ms in normal operation. Your ComplianceAuditView projection may lag up to 2 seconds. These are not arbitrary numbers — they are service-level objectives (SLOs) you define in your DESIGN.md and demonstrate in testing. A projection system with no lag measurement is not production-ready. Your daemon must expose get\_lag() for every projection it manages, and your test suite must assert that lag stays within bounds under a simulated load of 50 concurrent command handlers.

# **PHASE 4  ·  Upcasting, Integrity & The Gas Town Memory Pattern**

## **4A — Upcaster Registry**

Implement a centralized UpcasterRegistry that automatically applies version migrations whenever old events are loaded from the store. The event loading path must call the registry transparently — callers never manually invoke upcasters.

class UpcasterRegistry:  
    def \_\_init\_\_(self):  
        self.\_upcasters: dict\[tuple\[str, int\], Callable\] \= {}

    def register(self, event\_type: str, from\_version: int):  
        """Decorator. Registers fn as upcaster from event\_type@from\_version."""  
        def decorator(fn: Callable\[\[dict\], dict\]) \-\> Callable:  
            self.\_upcasters\[(event\_type, from\_version)\] \= fn  
            return fn  
        return decorator

    def upcast(self, event: StoredEvent) \-\> StoredEvent:  
        """Apply all registered upcasters for this event type in version order."""  
        current \= event  
        v \= event.event\_version  
        while (event.event\_type, v) in self.\_upcasters:  
            new\_payload \= self.\_upcasters\[(event.event\_type, v)\](current.payload)  
            current \= current.with\_payload(new\_payload, version=v \+ 1\)  
            v \+= 1  
        return current

\# Usage:  
registry \= UpcasterRegistry()

@registry.register("CreditAnalysisCompleted", from\_version=1)  
def upcast\_credit\_v1\_to\_v2(payload: dict) \-\> dict:  
    return {  
        \*\*payload,  
        "model\_version": "legacy-pre-2026",   \# inference for historical events  
        "confidence\_score": None,              \# genuinely unknown — do not fabricate  
    }

Implement upcasters for the following events and justify your inference strategy for missing historical fields in DESIGN.md:

1. **CreditAnalysisCompleted v1→v2:** Add model\_version (inferred from recorded\_at timestamp), confidence\_score (null — genuinely unknown; document why fabrication would be worse than null), regulatory\_basis (infer from rule versions active at recorded\_at date).  
2. **DecisionGenerated v1→v2:** Add model\_versions{} dict (reconstruct from contributing\_agent\_sessions by loading each session's AgentContextLoaded event — this requires a store lookup; document the performance implication).

### **The Immutability Test**

Your test suite must include a test that: (1) directly queries the events table in Postgres to get the raw stored payload of a v1 event, (2) loads the same event through your EventStore.load\_stream() and verifies it is upcasted to v2, (3) directly queries the events table again and verifies the raw stored payload is UNCHANGED. Any system where upcasting touches the stored events has broken the core guarantee of event sourcing. This test is mandatory and will be run during assessment.

## **4B — Cryptographic Audit Chain**

Regulatory-grade audit trails require tamper evidence. Implement a hash chain over the event log for the AuditLedger aggregate. Each AuditIntegrityCheckRun event records a hash of all preceding events plus the previous integrity hash, forming a blockchain-style chain. Any post-hoc modification of events breaks the chain.

async def run\_integrity\_check(  
    store: EventStore,  
    entity\_type: str,  
    entity\_id: str,  
) \-\> IntegrityCheckResult:  
    """  
    1\. Load all events for the entity's primary stream  
    2\. Load the last AuditIntegrityCheckRun event (if any)  
    3\. Hash the payloads of all events since the last check  
    4\. Verify hash chain: new\_hash \= sha256(previous\_hash \+ event\_hashes)  
    5\. Append new AuditIntegrityCheckRun event to audit-{entity\_type}-{entity\_id} stream  
    6\. Return result with: events\_verified, chain\_valid (bool), tamper\_detected (bool)  
    """

## **4C — The Gas Town Agent Memory Pattern**

Implement the pattern that prevents the catastrophic memory loss described in the program materials. An AI agent that crashes mid-session must be able to restart and reconstruct its exact context from the event store, then continue where it left off without repeating completed work.

async def reconstruct\_agent\_context(  
    store: EventStore,  
    agent\_id: str,  
    session\_id: str,  
    token\_budget: int \= 8000,  
) \-\> AgentContext:  
    """  
    1\. Load full AgentSession stream for agent\_id \+ session\_id  
    2\. Identify: last completed action, pending work items, current application state  
    3\. Summarise old events into prose (token-efficient)  
    4\. Preserve verbatim: last 3 events, any PENDING or ERROR state events  
    5\. Return: AgentContext with context\_text, last\_event\_position,  
               pending\_work\[\], session\_health\_status

    CRITICAL: if the agent's last event was a partial decision (no corresponding  
    completion event), flag the context as NEEDS\_RECONCILIATION — the agent  
    must resolve the partial state before proceeding.  
    """

Test this pattern with a simulated crash: start an agent session, append 5 events, then call reconstruct\_agent\_context() without the in-memory agent object. Verify that the reconstructed context contains enough information for the agent to continue correctly.

# **PHASE 5  ·  MCP Server — Exposing The Ledger as Enterprise Infrastructure** 

The MCP server is the interface between The Ledger and any AI agent or enterprise system that needs to interact with it. Tools (Commands) write events; Resources (Queries) read from projections. This is structural CQRS — the MCP specification naturally implements the read/write separation.

## **MCP Tools — The Command Side**

| TOOL NAME | COMMAND IT EXECUTES | CRITICAL VALIDATION | RETURN VALUE |
| :---- | :---- | :---- | :---- |
| submit\_application | ApplicationSubmitted | Schema validation via Pydantic; duplicate application\_id check | stream\_id, initial\_version |
| record\_credit\_analysis | CreditAnalysisCompleted | agent\_id must have active AgentSession with context loaded; optimistic concurrency on loan stream | event\_id, new\_stream\_version |
| record\_fraud\_screening | FraudScreeningCompleted | Same agent session validation; fraud\_score must be 0.0–1.0 | event\_id, new\_stream\_version |
| record\_compliance\_check | ComplianceRulePassed / ComplianceRuleFailed | rule\_id must exist in active regulation\_set\_version | check\_id, compliance\_status |
| generate\_decision | DecisionGenerated | All required analyses must be present; confidence floor enforcement | decision\_id, recommendation |
| record\_human\_review | HumanReviewCompleted | reviewer\_id authentication; if override=True, override\_reason required | final\_decision, application\_state |
| start\_agent\_session | AgentContextLoaded | Gas Town: required before any agent decision tools; writes context source and token count | session\_id, context\_position |
| run\_integrity\_check | AuditIntegrityCheckRun | Can only be called by compliance role; rate-limited to 1/minute per entity | check\_result, chain\_valid |

## **MCP Resources — The Query Side**

Resources expose projections. They must never load aggregate streams — all reads must come from projections. A resource that replays events on every query is an anti-pattern that will not scale.

| RESOURCE URI | PROJECTION SOURCE | SUPPORTS TEMPORAL QUERY? | SLO |
| :---- | :---- | :---- | :---- |
| ledger://applications/{id} | ApplicationSummary | No — current state only | p99 \< 50ms |
| ledger://applications/{id}/compliance | ComplianceAuditView | Yes — ?as\_of=timestamp | p99 \< 200ms |
| ledger://applications/{id}/audit-trail | AuditLedger stream (direct load — justified exception) | Yes — ?from=\&to= range | p99 \< 500ms |
| ledger://agents/{id}/performance | AgentPerformanceLedger | No — current metrics only | p99 \< 50ms |
| ledger://agents/{id}/sessions/{session\_id} | AgentSession stream (direct load) | Yes — full replay capability | p99 \< 300ms |
| ledger://ledger/health | ProjectionDaemon.get\_all\_lags() | No | p99 \< 10ms — this is the watchdog endpoint |

## **Tool Interface Design for LLM Consumption**

Tools and resources are consumed by AI agents, not humans. The description and parameter schema of each tool determines whether the consuming LLM uses it correctly — this is API design for a non-human consumer. Two requirements that most engineers miss:

* **Precondition documentation in the tool description:** "This tool requires an active agent session created by start\_agent\_session. Calling without an active session will return a PreconditionFailed error." An LLM that does not know this precondition will repeatedly fail and retry. The description is the only contract the LLM has.  
* **Structured error types, not messages:** Errors returned by tools must be typed objects: {error\_type: "OptimisticConcurrencyError", message: "...", stream\_id: "...", expected\_version: 3, actual\_version: 5, suggested\_action: "reload\_stream\_and\_retry"}. An LLM that receives an unstructured error message cannot reason about what to do. A typed error with suggested\_action enables autonomous recovery.

## **The MCP Integration Test**

Your MCP server must pass this integration test: start a fresh Ledger instance, then drive a complete loan application lifecycle — from ApplicationSubmitted through FinalApproved — using only MCP tool calls. No direct Python function calls. The test simulates what a real AI agent would do: it calls start\_agent\_session, then record\_credit\_analysis, then generate\_decision, then record\_human\_review, then queries the compliance audit view to verify the complete trace is present. If any step requires a workaround outside the MCP interface, the interface has a design flaw.

# **PHASE 6 (BONUS)  ·  What-If Projections & Regulatory Time Travel**

This phase is required for Score 5 and is the discriminator for trainees with genuine event sourcing experience. It is challenging and takes a full day. Attempt it only after Phases 1–5 are solid.

## **The What-If Projector**

The Apex compliance team needs to run counterfactual scenarios: "What would the decision have been if we had used the March risk model instead of the February risk model?" This requires replaying application history with a substituted event — a counterfactual — injected at the point of the original credit analysis.

async def run\_what\_if(  
    store: EventStore,  
    application\_id: str,  
    branch\_at\_event\_type: str,            \# e.g. "CreditAnalysisCompleted"  
    counterfactual\_events: list\[BaseEvent\],  \# events to inject instead of real ones  
    projections: list\[Projection\],        \# projections to evaluate under the scenario  
) \-\> WhatIfResult:  
    """  
    1\. Load all events for the application stream up to the branch point  
    2\. At the branch point, inject counterfactual\_events instead of real events  
    3\. Continue replaying real events that are causally INDEPENDENT of the branch  
    4\. Skip real events that are causally DEPENDENT on the branched events  
    5\. Apply all events (pre-branch real \+ counterfactual \+ post-branch independent)  
       to each projection  
    6\. Return: {real\_outcome, counterfactual\_outcome, divergence\_events\[\]}

    NEVER writes counterfactual events to the real store.  
    Causal dependency: an event is dependent if its causation\_id traces  
    back to an event at or after the branch point.  
    """

Demonstrate with the specific scenario: "What would the final decision have been if the credit analysis had returned risk\_tier='HIGH' instead of 'MEDIUM'?" Your what-if projector must produce a materially different ApplicationSummary outcome — demonstrating that business rule enforcement cascades correctly through the counterfactual.

## **Regulatory Examination Package**

Implement a generate\_regulatory\_package(application\_id, examination\_date) function that produces a complete, self-contained examination package containing:

1. The complete event stream for the application, in order, with full payloads.  
2. The state of every projection as it existed at examination\_date.  
3. The audit chain integrity verification result.  
4. A human-readable narrative of the application lifecycle, generated by replaying events and constructing a plain-English summary (one sentence per significant event).  
5. The model versions, confidence scores, and input data hashes for every AI agent that participated in the decision.

The package must be a self-contained JSON file that a regulator can verify against the database independently — they should not need to trust your system to validate that the package is accurate.

# **DESIGN.md — Required Sections**

This document is assessed with equal weight to the code. The principle: architecture is about tradeoffs. A decision without a tradeoff analysis is not an architectural decision — it is a default. Six required sections:

1. **Aggregate boundary justification:** Why is ComplianceRecord a separate aggregate from LoanApplication? What would couple if you merged them? Trace the coupling to a specific failure mode under concurrent write scenarios.  
2. **Projection strategy:** For each projection, justify: Inline vs. Async, and the SLO commitment. For the ComplianceAuditView temporal query, justify your snapshot strategy (event-count trigger, time trigger, or manual) and describe snapshot invalidation logic.  
3. **Concurrency analysis:** Under peak load (100 concurrent applications, 4 agents each), how many OptimisticConcurrencyErrors do you expect per minute on the loan-{id} streams? What is the retry strategy and what is the maximum retry budget before you return a failure to the caller?  
4. **Upcasting inference decisions:** For every inferred field in your upcasters, quantify the likely error rate and the downstream consequence of an incorrect inference. When would you choose null over an inference?  
5. **EventStoreDB comparison:** Map your PostgreSQL schema to EventStoreDB concepts: streams → stream IDs, your load\_all() → EventStoreDB $all stream subscription, your ProjectionDaemon → EventStoreDB persistent subscriptions. What does EventStoreDB give you that your implementation must work harder to achieve?  
6. **What you would do differently:** Name the single most significant architectural decision you would reconsider with another full day. This section is the most important — it shows whether you can distinguish between "what I built" and "what the best version of this would be."

# **Deliverables**

## **Interim — Sunday March 22, 03:00 UTC**

### **GitHub Code:**

* `src/schema.sql` — PostgreSQL schema: `events`, `event_streams`, `projection_checkpoints`, `outbox` tables with all indexes and constraints  
* `src/event_store.py` — `EventStore` async class with `append`, `load_stream`, `load_all`, `stream_version`, `archive_stream`, `get_stream_metadata`; optimistic concurrency enforced via `expected_version`  
* `src/models/events.py` — Pydantic models for all event types in the Event Catalogue (`BaseEvent`, `StoredEvent`, `StreamMetadata`) plus custom exceptions (`OptimisticConcurrencyError`, `DomainError`)  
* `src/aggregates/loan_application.py` — `LoanApplicationAggregate` with state machine, event replay via `load()`, and `_apply` handlers for all loan lifecycle events  
* `src/aggregates/agent_session.py` — `AgentSessionAggregate` with Gas Town context enforcement and model version tracking  
* `src/commands/handlers.py` — Command handlers following the load → validate → determine → append pattern (at minimum: `handle_credit_analysis_completed`, `handle_submit_application`)  
* `tests/test_concurrency.py` — Double-decision concurrency test: two concurrent `asyncio` tasks appending to the same stream at `expected_version=3`; asserts exactly one succeeds, one raises `OptimisticConcurrencyError`, and total stream length \= 4  
* `pyproject.toml` with locked deps (uv)  
* `README.md` — how to install, run migrations, and execute the test suite

### **Single PDF Report containing:**

1. `DOMAIN_NOTES.md` content (complete, as graded deliverable)  
2. Architecture diagram showing event store schema, aggregate boundaries, and command flow  
3. Progress summary: what is working (Phase 1 \+ Phase 2), what is in progress  
4. Concurrency test results: screenshot or log output of the double-decision test passing  
5. Known gaps and plan for final submission

## **Final — Thursday March 26, 03:00 UTC**

### **GitHub Code (full system):**

**Phase 1 — Event Store Core:**

* `src/schema.sql` — Full PostgreSQL schema with all tables, indexes, and constraints  
* `src/event_store.py` — Complete `EventStore` async class with all interface methods, outbox writes in same transaction, stream archival support  
* `src/models/events.py` — All Pydantic models: event types, stored event wrapper, stream metadata, error types

**Phase 2 — Domain Logic:**

* `src/aggregates/loan_application.py` — `LoanApplicationAggregate` with full state machine (Submitted → AwaitingAnalysis → AnalysisComplete → ComplianceReview → PendingDecision → ApprovedPendingHuman / DeclinedPendingHuman → FinalApproved / FinalDeclined), all 6 business rules enforced  
* `src/aggregates/agent_session.py` — `AgentSessionAggregate` with Gas Town context enforcement, model version locking  
* `src/aggregates/compliance_record.py` — `ComplianceRecordAggregate` with mandatory check tracking and regulation version references  
* `src/aggregates/audit_ledger.py` — `AuditLedgerAggregate` with append-only enforcement and cross-stream causal ordering  
* `src/commands/handlers.py` — All command handlers: submit\_application, credit\_analysis\_completed, fraud\_screening\_completed, compliance\_check, generate\_decision, human\_review\_completed, start\_agent\_session

**Phase 3 — Projections & Async Daemon:**

* `src/projections/daemon.py` — `ProjectionDaemon` with fault-tolerant batch processing, per-projection checkpoint management, configurable retry, and `get_lag()` per projection  
* `src/projections/application_summary.py` — `ApplicationSummary` projection (one row per application, current state)  
* `src/projections/agent_performance.py` — `AgentPerformanceLedger` projection (metrics per agent model version)  
* `src/projections/compliance_audit.py` — `ComplianceAuditView` projection with temporal query support (`get_compliance_at(application_id, timestamp)`), snapshot strategy, and `rebuild_from_scratch()`

**Phase 4 — Upcasting, Integrity & Gas Town:**

* `src/upcasting/registry.py` — `UpcasterRegistry` with automatic version chain application on event load  
* `src/upcasting/upcasters.py` — Registered upcasters: `CreditAnalysisCompleted` v1→v2, `DecisionGenerated` v1→v2, with inference strategies documented  
* `src/integrity/audit_chain.py` — `run_integrity_check()`: SHA-256 hash chain construction, tamper detection, chain verification  
* `src/integrity/gas_town.py` — `reconstruct_agent_context()`: agent memory reconstruction from event stream with token budget, `NEEDS_RECONCILIATION` detection

**Phase 5 — MCP Server:**

* `src/mcp/server.py` — MCP server entry point  
* `src/mcp/tools.py` — 8 MCP tools (command side): `submit_application`, `record_credit_analysis`, `record_fraud_screening`, `record_compliance_check`, `generate_decision`, `record_human_review`, `start_agent_session`, `run_integrity_check`; all with structured error types and precondition documentation in tool descriptions  
* `src/mcp/resources.py` — 6 MCP resources (query side): `ledger://applications/{id}`, `ledger://applications/{id}/compliance`, `ledger://applications/{id}/audit-trail`, `ledger://agents/{id}/performance`, `ledger://agents/{id}/sessions/{session_id}`, `ledger://ledger/health`; all reading from projections (no stream replays except justified exceptions)

**Phase 6 (Bonus):**

* `src/what_if/projector.py` — `run_what_if()`: counterfactual event injection with causal dependency filtering, never writes to real store  
* `src/regulatory/package.py` — `generate_regulatory_package()`: self-contained JSON examination package with event stream, projection states at examination date, integrity verification, human-readable narrative, and agent model metadata

**Tests:**

* `tests/test_concurrency.py` — Double-decision test (two concurrent appends, exactly one succeeds)  
* `tests/test_upcasting.py` — Immutability test: v1 event stored, loaded as v2 via upcaster, raw DB payload confirmed unchanged  
* `tests/test_projections.py` — Projection lag SLO tests under simulated load of 50 concurrent command handlers; `rebuild_from_scratch` test  
* `tests/test_gas_town.py` — Simulated crash recovery: 5 events appended, `reconstruct_agent_context()` called without in-memory agent, verify reconstructed context is sufficient to continue  
* `tests/test_mcp_lifecycle.py` — Full loan application lifecycle driven entirely through MCP tool calls: `start_agent_session` → `record_credit_analysis` → `record_fraud_screening` → `record_compliance_check` → `generate_decision` → `record_human_review` → query `ledger://applications/{id}/compliance` to verify complete trace  
* `pyproject.toml` with locked deps (uv)  
* `README.md` — Full setup instructions: database provisioning, migration, running all phases, MCP server startup, and query examples

### **Single PDF Report containing:**

1. `DOMAIN_NOTES.md` content (complete, finalized)  
2. `DESIGN.md` content (complete, finalized)  
3. Architecture diagram: event store schema, aggregate boundaries, projection data flow, MCP tool/resource mapping  
4. Concurrency & SLO analysis: double-decision test results, projection lag measurements under load, retry budget analysis  
5. Upcasting & integrity results: immutability test output, hash chain verification output, tamper detection demonstration  
6. MCP lifecycle test results: full loan application trace from `ApplicationSubmitted` through `FinalApproved` via MCP tools only  
7. Bonus results (if attempted): what-if counterfactual outcome comparison, regulatory package sample output  
8. Limitations & reflection: what the implementation does not handle, what you would change with more time

### **Video Demo (max 6 min):**

**Minutes 1–3 (Required):**

* **Step 1 — The Week Standard:** Run "Show me the complete decision history of application ID X" end-to-end. Show full event stream, all agent actions, compliance checks, human review, causal links, and cryptographic integrity verification. Time it — must complete in under 60 seconds.  
* **Step 2 — Concurrency Under Pressure:** Run the double-decision test live. Show two agents colliding on the same stream, one succeeding, one receiving `OptimisticConcurrencyError` and retrying.  
* **Step 3 — Temporal Compliance Query:** Query `ledger://applications/{id}/compliance?as_of={timestamp}` for a past point in time. Show the compliance state as it existed at that moment, distinct from the current state.

**Minutes 4–6 (Mastery):**

* **Step 4 — Upcasting & Immutability:** Load a v1 event through the store, show it arrives as v2. Query the raw database row and show the stored payload is unchanged.  
* **Step 5 — Gas Town Recovery:** Start an agent session, append several events, simulate a crash (kill the process). Call `reconstruct_agent_context()` and show the agent can resume with correct state.  
* **Step 6 — What-If Counterfactual (Bonus):** Run a what-if scenario substituting a HIGH risk tier for MEDIUM. Show the cascading effect on the final decision through business rule enforcement.


# **Assessment Rubric**

*Score 3 \= functional and demonstrates understanding. Score 5 \= production-ready, would deploy to a real enterprise client. Scores 4 and 5 require demonstrated understanding in DESIGN.md, not just working code.*

| CRITERION | 1 | 2 | 3 | 4 | 5 |
| :---- | :---- | :---- | :---- | :---- | :---- |
| **Event Store Core & Concurrency** | Schema present; no concurrency control | Append works; expected\_version not enforced | All interface methods; concurrency enforced; double-decision test passes | Outbox pattern; archive support; all edge cases; concurrent load test passes | Above \+ DESIGN.md justifies every schema column; retry strategy documented with error rate estimate |
| **Domain Logic & Business Rules** | One aggregate; no state machine | State machine present; some rules missing | Both aggregates; all 6 business rules enforced | Causal chain enforcement; Gas Town pattern; model version locking | Above \+ counterfactual command testing; all invariants tested under concurrent scenarios |
| **Projection Daemon & CQRS** | No projections or direct stream reads only | One projection; no lag metric | All 3 projections; lag metric exposed; daemon fault-tolerant | SLO tests passing; rebuild-from-scratch without downtime; temporal query on ComplianceAuditView | Above \+ snapshot invalidation; distributed daemon analysis in DESIGN.md |
| **Upcasting & Integrity** | No upcasting; store mutated on upcast | Upcaster exists; chain not automatic | Auto-upcasting via registry; immutability test passes | Both upcasters; inference justified; null vs. fabrication reasoning present | Above \+ hash chain integrity; generate\_regulatory\_package working; chain break detection |
| **MCP Server — Tool Design** | No MCP server | Tools present; error types unstructured | All 8 tools; structured errors; preconditions documented | Resources from projections (no stream reads in resources); SLOs met | Above \+ full lifecycle integration test via MCP only; LLM-consumption preconditions in all tool descriptions |
| **DESIGN.md — Architectural Reasoning** | Not present | Describes what was built; no tradeoff analysis | All 6 sections; tradeoffs identified | Quantitative analysis (error rates, lag SLOs, retry budgets) | "What I would do differently" shows genuine reflection; identifies the thing the implementation got wrong |
| **BONUS — What-If & Regulatory Package** | (Not attempted) | (Not attempted) | What-if projection working on test scenario | Counterfactual produces materially different outcome; causal dependency filtering correct | Above \+ regulatory package is independently verifiable; narrative generation coherent |

[image1]: <data:image/png;base64,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>