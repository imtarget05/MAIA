# Leave Policy v2.4 — HR Department

## Section 1: Entitlement
Each employee has 12 annual leave days per year. Unused days may carry over up to 3 days to next year if approved. Sick leave is separate: up to 30 days with medical certificate.

## Section 2: Request Rules
- 1-2 days leave: request at least 3 working days in advance.
- 3-5 days leave: request at least 7 working days in advance.
- >5 days leave: request at least 14 days in advance and requires HR director approval.

## Section 3: Approval Flow
Step 1: Employee creates leave request via MAIA or HR portal.
Step 2: System validates remaining balance via `check_leave_balance`.
Step 3: Manager approves/rejects within 48 hours.
Step 4: HR confirms and updates balance. Request ID is returned (e.g., LV-20260910-001).

## Section 4: Balance Check
Employees can ask MAIA "số ngày phép còn lại" to check balance. The system queries HR database and returns remaining days.

## Section 5: Special Leave
Maternity, paternity, and bereavement leave follow separate policy documents.
