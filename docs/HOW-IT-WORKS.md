# How it works: one order, hop by hop

This follows one CT order from the moment the RIS sends it until the FHIR server holds a ServiceRequest, and
then the report that comes back. For each hop: what the code does, why it's built that way, and what breaks
in the field.

```
 RIS / reporting ──MLLP──▶ LISTENER ──▶ PARSE ──▶ DEDUPE ──▶ MAP ──▶ VALIDATE ──▶ SINK ──▶ ACK
   (TCP 2575)              mllp.py      hl7/      bridge.py  mapping/ validate.py  sink.py   hl7/ack.py
                                                                          │
                                              data/bundles/*.json ◀───────┤
                                              FHIR server  POST /  ◀──────┘  (transaction Bundle)
```

---

## FHIR for someone who knows v2

If you know v2, most of FHIR is familiar. It is a different packaging with stronger opinions about identity.

| v2 idea | FHIR idea |
|---|---|
| a message (ADT^A04) is an *event* | the server stores *resources* (Patient, ServiceRequest…); the event is how they change |
| segment (PID, ORC, OBR) | resource (Patient, ServiceRequest, DiagnosticReport), roughly one-to-one |
| PID-3 with assigning authority | `Patient.identifier` with `system` (a URI naming the authority) + `value` |
| the receiving system matches on the MRN | *you* tell the server how to match: `PUT Patient?identifier=system\|MRN` (conditional update) |
| an A08 overwrites the patient | `PUT` replaces the whole resource (anything missing is deleted) |
| a table value (0001: M/F) | a `code` from a code system (AdministrativeGender: male/female) |
| MSH-10 control id | nothing built in: this bridge writes a **Provenance** per message and `meta.source` on every resource |
| ACK AA/AE/AR | HTTP status of the transaction (200 with a per-entry result, or 4xx/5xx for the whole thing) |
| a v2 batch (FHS/BHS; each message stands alone) | a FHIR **batch** Bundle (each entry processed on its own). This bridge sends one message as one **transaction** Bundle instead: every entry succeeds or none do |

The two things that matter most for an interface:

1. **Conditional requests make replays harmless.** `POST ServiceRequest` + `ifNoneExist: identifier=…` means
   "create it unless one with this identifier exists". A resent ORM matches and changes nothing. Engines
   resend a lot, so this is the difference between one order and five.
2. **Inside a transaction, resources point at each other by temporary ids** (`urn:uuid:…`). The
   ServiceRequest's `subject` points at the Patient entry's `urn:uuid`. The server first works out which real
   Patient that is (existing or new), then rewrites the reference. So the bridge never needs to know a
   server id. (It does read orders and reports before writing them, for a different reason: see Hop 5.)

---

## Hop 1: the RIS sends an ORM over MLLP (`tools/ris_sim.py`, `v2fhir/mllp.py`)

**What happens.** The RIS connects to port 2575 and sends `0x0B` + message + `0x1C 0x0D`, then waits for an ACK
before sending the next message.

**The code.** `FrameDecoder.feed()` gets whatever `read()` returned: half a frame, three frames, or the `0x1C` of
one frame with its `0x0D` in the next read. It returns complete frames. Bytes outside a frame (keep-alives,
line noise) are counted and dropped. A new `0x0B` before the end abandons the partial frame. A frame over
`max_message_bytes` keeps only its first 4 KB, enough to NAK with the right MSA-2, so memory stays bounded.
The search resumes where it stopped, and never looks past the next `0x0B`, so a 10 MB report (the default
`max_message_bytes`) arriving in 64 KB reads, or a flood of `0x0B` bytes, isn't O(n²).

**The listener.** One asyncio task per connection, on the selector event loop on every OS (on Windows the
default Proactor loop closes the listening socket when a single client resets during accept). Messages on a
connection are handled one at a time, in order: HL7 ordering matters (an A08 must not overtake its A04). Each
message runs on a worker thread, but the FHIR part (the reads before writing and the transaction) runs one
message at a time across all connections, so the read-then-write checks can't race each other. The price is
that a slow FHIR server delays every sender's ACK, so each message has a deadline (`ack_deadline_seconds`,
default 25 s, under a typical 30 s sender timeout): when it runs out the sender gets AR and resends later. A peer
that goes quiet, stops reading its ACKs, or joins a crowd of silent connections is dropped (idle timeout,
`max_connections`). Nothing a message or a connection does can stop the
listener: `tests/test_adversarial.py` throws random bytes, truncated frames, oversize frames and hang-ups at it
from four threads, then checks that it still ACKs a good message.

## Hop 2: bytes become a message (`v2fhir/hl7/charset.py`, `parser.py`)

**Character set first.** MSH is always ASCII, so MSH-18 can be read before decoding. If it is declared, the bytes
are decoded strictly: MSH-18 saying ASCII while the bytes are Latin-1 is an **AE**, because guessing would store
"MU?OZ" on a real patient. Latin-1 accepts every byte, so a sender that says 8859/1 but sends UTF-8 can't
be caught that way; the bridge checks whether the bytes are valid multi-byte UTF-8 instead, and that is an AE
too. If MSH-18 is empty (most senders), the bridge tries UTF-8, then cp1252, then Latin-1, and warns when it
had to fall back.

**Then structure.** Delimiters come from MSH-1/MSH-2 and are never assumed. Segments end at CR. If the message
contains no CR, LF is taken as the terminator; if it does, a bare LF is data (a line break a reporting system
left inside report text), not a new segment. Values stay escaped until read, so `\F\` can never split a
field. The two-character value `""` (HL7 "delete this") is distinguished from an empty field.

**Field reality.** Everything the parser refuses (no MSH, a garbage segment id, a second MSH, a missing MSH-9,
MSH-10 or MSH-12) becomes an **AR** with an ERR segment. The garbage is never used as the ERR-2 location (a bad segment id there
split the ERR segment, one of the fuzzing findings); ERR-8 and MSA-3 quote a few characters of it, escaped with
the sender's delimiters and stripped of control characters.

## Hop 3: have we seen it? (`v2fhir/bridge.py`)

The dedupe key is sending app + facility + MSH-10; the check is a hash of the message with MSH-7 blanked. Same
key and same content means a resend: the sender gets AA again and nothing is sent. Same key with *different*
content means a sender reusing control ids (counters reset on restart). That message is processed, with a
warning, because dropping a real message is worse than a duplicate the FHIR side absorbs anyway. The cache is
in memory; after a restart, idempotency on the FHIR side (Hop 5) takes over.

## Hop 4: v2 becomes FHIR (`v2fhir/mapping/`)

Details in [MAPPING.md](MAPPING.md). For this ORM^O01 NW:

```
POST Patient         ifNoneExist identifier=…/synth-hosp|SYN100234          (orders never overwrite demographics)
POST Practitioner    ifNoneExist identifier=…/synth-provider|1001           (ORC-12, OBR-16 and PV1-7: one entry)
POST Encounter       ifNoneExist identifier=…/synth-hosp|V900001
POST ServiceRequest  ifNoneExist identifier=…/synth-his|ORD1001,…/synth-ris|FIL5001,…/accession|ACC2001
PUT  Provenance/v2-4fd5f894…                                                 (id from sender + MSH-10 + content)
```

**The choices that matter**

* *Who owns what.* ADT upserts Patient (it is the source of truth). Orders and results only create-if-absent,
  so a stale PID on an old order can't undo a newer A08.
* *NW is create-if-absent.* A replayed NW changes nothing, and neither does an NW that arrives after its own
  result (ORU before ORM): it can't re-open a reported order.
* *Status-only messages patch.* An ORC-only cancel becomes a FHIRPath Patch of `ServiceRequest.status`. A PUT
  would replace the order with one that has no procedure and no requester. The order's `meta.source` still names
  the NW; the cancel is traced through its own Provenance.
* *Orders match on any of their numbers.* The HIS sends NW with a placer number, and the report comes back
  with filler + accession. `identifier=placer,filler,accession` finds the order either way. Two different
  orders matching is a 412 and an AE, because a person has to look. So is one order whose placer, filler or
  accession disagrees with this message's (a different order sharing one number): AE 205. And an order that
  belongs to another patient is never written to: AE, unless an A40 merge links the two patients.

## Hop 5: validate, write, send (`v2fhir/validate.py`, `v2fhir/sink.py`)

The bundle is validated with `fhir.resources` (structure, data types, required elements, primitive formats),
plus two things the models don't do: the required-binding codes (`gender: "bogus"` passes the models), and the
handful of FHIR invariants this mapper could break (per-1, prr-1, bdl-7, no empty elements, 1 MB strings). Other
invariants and profiles are not evaluated. An invalid bundle is an AE and is never sent.

It is written to `data/bundles/<sender>_<facility>_<control id>-<name hash>-<content hash>.json`: an identical
resend overwrites its own file, and a reused control id gets a new one. The name is sanitised, because MSH-10 comes
from the network and `../../x` is data, not a path. Then it is POSTed to the FHIR server. Connection errors
and 5xx are retried with backoff; 4xx are not (the same bundle would be refused again).

Before the POST, every order and report entry is checked against what the server holds, because a
conditional PUT cannot say "only if newer". A report never goes down in status (a late preliminary after the
final, a resent final after the correction, a queued cancel after the final), and at the same status the
report with the older OBR-22 loses. An order that is completed or revoked is not re-opened by an older
status message. The stale entry is dropped with a warning: a retried old message must not un-sign a report.

## Hop 6: the ACK (`v2fhir/hl7/ack.py`)

AA only after the bundle is on disk *and* accepted by the server. An AA means "you can forget this message".
AR and AE both mean "keep it". AR is for problems that have nothing to do with the message's content, including
"the FHIR server is down, try again later" (v2.5.1 2.9.2.2); AE is for problems in the content itself. Resending is
safe because every request is conditional and orders and reports are checked before writing. The ACK uses the sender's own
delimiters, echoes MSH-11/12/18, and puts warnings in ERR segments with severity W, but only for v2.5+, where
ERR-4 exists. A v2.3 receiver would read any ERR as a failure. Control characters from the inbound message
are never echoed: a stray `0x0B` or `0x1C` in an ACK breaks the sender's MLLP framing.

---

## Then the report comes back

```
ORU^R01 P  ->  PUT DiagnosticReport?identifier=…   (created, version 1, preliminary)
ORU^R01 F  ->  PUT DiagnosticReport?identifier=…   (same resource, version 2, final)
ORU^R01 C  ->  PUT DiagnosticReport?identifier=…   (version 3, corrected)
```

One report, three versions. `DiagnosticReport/_history` shows preliminary → final → corrected, each version
with `meta.source` naming the message that wrote it. `basedOn` points at the order, `imagingStudy` at the
study found by its Study Instance UID (ZDS-1), `conclusion` is the impression, and `presentedForm` is the whole
report text.
