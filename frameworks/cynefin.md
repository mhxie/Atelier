# Cynefin Framework

Origin: Dave Snowden (Cognitive Edge). A sense-making framework for understanding what type of situation you're in — because different situations require fundamentally different approaches.

## When to Use

- Unsure whether to plan carefully or just experiment
- Feeling like your approach doesn't match the problem
- Managing a situation that keeps surprising you
- Team disagreement about how to tackle something

## The Five Domains

### 1. Clear (formerly "Obvious")
**Characteristics:** Cause and effect are obvious. Best practices exist.
**Approach:** Sense → Categorize → Respond
**Example:** "How do I format this PR?" — there's a clear standard, follow it.
**Danger:** Complacency. Clear domains can shift to chaotic without warning.

### 2. Complicated
**Characteristics:** Cause and effect exist but require expertise to see. Multiple right answers.
**Approach:** Sense → Analyze → Respond
**Example:** "How should we architect this distributed system?" — requires expertise, but analyzable.
**Danger:** Analysis paralysis. Experts may disagree, and that's okay.

### 3. Complex
**Characteristics:** Cause and effect only visible in retrospect. Emergent patterns. No right answers, only better ones.
**Approach:** Probe → Sense → Respond (experiment first, then learn)
**Example:** "Will this career move work out?" — too many variables, probe with small experiments.
**Danger:** Trying to analyze your way through complexity. You can't — you must experiment.

### 4. Chaotic
**Characteristics:** No perceivable cause and effect. Turbulence. Must act first.
**Approach:** Act → Sense → Respond (stabilize first, then figure out what happened)
**Example:** Critical production outage, layoff announced, health emergency.
**Danger:** Freezing. In chaos, any action that provides stability is better than analysis.

### 5. Confused (the center)
**Characteristics:** Don't know which domain you're in.
**Approach:** Break the situation into parts and classify each part separately.

## Decision Matrix

| Signal | You're Probably In | Do This |
|--------|-------------------|---------|
| Clear best practice exists | Clear | Follow it |
| Experts can analyze it | Complicated | Get expert input |
| Everyone has a different answer | Complex | Run small experiments |
| Everything is on fire | Chaotic | Act now, analyze later |
| Can't even frame the problem | Confused | Decompose into smaller parts |

## Common Mistake: Domain Mismatch

Most reflection failures come from treating a **complex** problem as if it were **complicated**:
- Trying to plan your way through a career transition (complex) like it's a database migration (complicated)
- Analyzing a relationship issue (complex) like it's a debugging problem (complicated)

The fix: if your analysis isn't producing clarity, you're probably in complex territory. Switch to probing.

## Pitfalls

- **Wanting everything to be complicated**: Engineers love analysis. But life is mostly complex.
- **Staying in probe mode too long**: At some point, commit.
- **Misclassifying chaos as complex**: Chaos needs immediate action, not experiments.

## Cross-validates with

- **OODA Loop** — OODA works in all domains but is essential in chaotic/complex
- **Double-Loop Learning** — When your approach keeps failing, you may be in the wrong domain
