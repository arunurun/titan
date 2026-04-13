TITAN V12.0: SYSTEM IMPLEMENTATION MANIFEST
Role: You are a Senior Backend Engineer and Quantitative Analyst.
Objective: Build a modular, tested, and automated market forensic pipeline.
Constraints:

Verification: You MUST self-reflect and cross-check every line of code before applying it.

Modularity: One function = One calculation.

Validation: 100% test coverage with corner-case edge testing.

State Management: Maintain a STATUS_LOG.md to track: NOT STARTED, IN PROGRESS, COMPLETED, BLOCKED.

PHASE 1: FOUNDATION & ENVIRONMENT
Task 1.1: Project Scaffolding
Initialize a Python 3.10+ environment.

Create directory structure: /src, /tests, /config.

Create requirements.txt with: breeze-connect, pandas, numpy, google-generativeai, supabase, pytest.

Self-Check: Verify package compatibility with Python 3.10.

Task 1.2: Secret Management & Configuration
Setup .env template for BREEZE_API_KEY, BREEZE_SECRET, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY.

Implement a config_loader.py to securely read these variables.

PHASE 2: THE CALCULATION ENGINE (src/titan_engine.py)
Instruction: Every function here must be pure. Input: Dataframe/Series. Output: Float/Boolean.

Task 2.1: Z-Score Module
Implement calculate_z_score(data, window=20).

Test Case: Verify output with a flat price (Z=0), a sharp spike (Z > 2), and empty data handles.

Task 2.2: Absorption Ratio Module
Implement calculate_absorption_ratio(current_delivery, avg_delivery_5d).

Test Case: Handle division by zero if volume is null; verify "Panic Absorption" trigger (>1.5x).

Task 2.3: Option Chain Forensic Module
Implement get_pcr(total_put_oi, total_call_oi).

Implement find_oi_walls(option_chain_df).

Test Case: Correctly identify the highest OI strike even if data is skewed by deep ITM spikes.

Task 2.4: Titan Intent Score Logic
Implement calculate_intent_score(pcr, z_score, absorption).

Logic: A weighted average that maps technicals to a 0-100 scale.

PHASE 3: THE BRAIN & CONTENT AGENT (src/brain.py)
Task 3.1: Gemini Integration
Implement generate_titan_narrative(audit_data).

Constraint: Inject the TITAN V12.0 Protocol as the System Instruction.

Verification: Before returning the post, the LLM must run a "Policy Check" to ensure NO targets or "Buy/Sell" words are used.

PHASE 4: DATA PIPELINE & SCHEDULING
Task 4.1: ICICI Breeze Connector
Implement fetch_nifty_data().

Retry Logic: If API fails, implement 3 retries with exponential backoff before marking task as BLOCKED.

Task 4.2: Supabase Persistence
Implement save_audit_log(payload).

Ensure timestamping is in IST (Asia/Kolkata).

Task 4.3: GitHub Actions Workflow
Create .github/workflows/market_audit.yml.

Schedule for 09:15 and 11:30 IST.

PHASE 5: FINAL INTEGRATION & DRY RUN
Task 5.1: The Controller (main.py)
Tie all modules together.

Manual Trigger Test: Run a full simulation using dummy data to verify the X/LinkedIn output formatting.

CURSOR OPERATIONAL PROTOCOL
Task Initiation: Before starting a task, update STATUS_LOG.md to IN PROGRESS.

Code Verification: Before "Applying" code, run pytest. If tests fail, you are BLOCKED.

Self-Correction: If BLOCKED, you must:

Analyze the traceback.

Research alternative library methods (e.g., if Breeze API has changed).

Present 2 alternative approaches to the user.

Content Audit: Every time you propose a social media post, you MUST run a internal "Compliance Scan" to ensure no SEBI-violating words (Buy, Sell, Target, SL) are present.