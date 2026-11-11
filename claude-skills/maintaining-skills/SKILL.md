---
name: maintaining-skills
description: House rules for writing, fixing and auditing Agent Skills so the library stays small, current and free of contradictions. Use whenever a skill is being created, updated, merged, renamed or deleted, when SKILL.md files are reviewed, or when the user asks which skills are out of date, overlapping, conflicting or no longer needed. Works alongside skill-creator, which handles evals and packaging.
---

# Maintaining skills

Every skill loads into the same context as every other skill. Two skills that state the same fact in different words will eventually drift apart, and then Claude has to guess which one is right. This skill keeps the library coherent as it grows. It covers three jobs: write a new skill, fix an existing one, audit the whole library.

For eval runs, benchmarking and description optimization, use `skill-creator`. This skill sets the standards that work is held to.

## First, read the library

Before writing or changing any skill, read the name and description of every installed skill, and read the full body of any skill that touches the same subject. Most problems come from not knowing a sibling skill already covers something.

## The rules, and why each one exists

**One home per fact.** Each fact, ID, policy or procedure lives in exactly one skill. Other skills point to it by name ("SKU format: see `inventory-intake`"). Duplicates drift, and drifted duplicates are the main source of contradictions.
- Exception: a one-line safety guard may be repeated where a skill can run on its own and a mistake is costly or hard to undo (wrong account ID, publish or send without approval). Keep the full rule in its home, and mark the repeat with "(home: `skill-name`)" so audits can see it is intentional.

**Give the goal and context, not a script, unless the work is fragile.** Explain what the output is for and what good looks like, then let the model choose the route. Use exact numbered steps only where order matters or a mistake breaks something: API payloads, approval gates, file operations, anything that touches money or live listings. Match how specific you are to how fragile the task is.

**Say what done looks like.** Every skill ends with a concrete finish line: the deliverable, its format, and the checks it must pass. This tells the model when to stop and when to keep going. A good finish line can be checked: "title ≤ 80 characters, count shown", not "high quality".

**Hard rules need evidence and a reason.** Add a hard "never" or "always" only when there is evidence one is needed: a past failure, a platform policy, a legal limit, or a costly mistake. Write the reason next to it in the same sentence. The model applies a rule correctly to cases the rule did not foresee only if it knows why the rule exists.

**Use plain, calm wording.** Write "Use X when…" rather than "CRITICAL: you MUST…". Recent models follow normal instructions closely, and shouted rules cause over-application. Use emphasis only after a test shows the model ignoring a normal instruction.

**Cut boilerplate the model already does.** Remove generic lines like "think carefully", "think step by step", "double-check your work" or "be thorough". Current models reason by default, and Anthropic's Opus 5 and 5.5 guidance is to remove these lines rather than rewrite them. A specific check is not boilerplate, so keep it: "scan every line for a price before delivering" names what to look for.

**Keep it small.**
- Write the description in the third person. Say what the skill does and when to use it, including the words a user would actually say. The description is the only part loaded before the skill triggers. Maximum 1,024 characters.
- Name: lowercase letters, numbers and hyphens, 64 characters or fewer, and the words "anthropic" and "claude" are not allowed.
- Keep the SKILL.md body under 500 lines. Move reference material (ID tables, long examples, policies) into files that SKILL.md links to directly, one level deep.
- Don't explain what the model already knows. Test each paragraph by asking "would a capable new hire need this?"

**Avoid time bombs.** Dates, prices, platform limits, event names and policy details go stale. Put them in one dated place ("as of 2026-11: Instagram allows 5 hashtags") so an audit can find them. Prefer "look up the current policy" over copying the policy in.

## Job 1: Write a new skill

1. Check the library for overlap first. If an existing skill covers 70% or more of the job, extend that skill instead of adding a new one.
2. Draft the skill against the rules above. Link to sibling skills for shared facts; don't copy them.
3. Before saving, push back. Tell the user which of their requested rules you disagree with, why, and what you would write instead. Then let them decide.
4. Test it on 2 or 3 realistic prompts (use skill-creator's eval loop when the output can be checked).

**Done when:** the skill passes the audit checklist below, it duplicates no facts from other skills, and the user has seen the pushback and approved the final text.

## Job 2: Fix an existing skill

Fix only what the request or audit found, and keep the author's voice and approved wording. When you remove a rule, say which rule and why. When a fact moves to another skill, update every skill that pointed to its old location in the same change.

**Done when:** the specific problems are fixed, no new contradiction was introduced (re-read the sibling skills), and the user has a short before/after list.

## Job 3: Audit the library

Read every SKILL.md and its reference files. Report findings in three groups, most costly first:

1. **Contradicts**: two skills, or two lines in one skill, give different answers to the same question. Quote both lines, name the files, and say which version should win and why.
2. **Out of date**: stale dates, event names, tools, product names, platform rules or limits. Flag what to verify; don't assert that something changed unless you checked a current source.
3. **Not needed anymore**: duplicate facts with no "(home: …)" marker, boilerplate, rules that no longer have evidence behind them, overlapping skills that should merge, and skills whose job another skill now does.

For each finding, give the fix in one line. End with the single highest-value fix to make first. Don't edit anything during an audit until the user picks which fixes to apply.

**Done when:** every skill has been read, each finding cites file and line, and the report fits on one screen per group.

## Audit checklist (use for every new or changed skill)

- Description is in the third person, says what the skill does and when to use it, and is 1,024 characters or fewer
- No fact, ID or rule duplicated from another skill (except marked safety guards)
- Each hard rule has its reason next to it
- No "think carefully / step by step / double-check" boilerplate; specific checks kept
- No shouting (ALL CAPS commands, CRITICAL, MUST) unless a test showed it was needed
- Has a checkable "done when"
- Strict steps only where the work is fragile
- Time-sensitive facts are dated and kept in one place
- Body is under 500 lines; references are one level deep
