-- ============================================================
-- ERP MONITORING - LOCAL DUMMY DATABASE
-- ============================================================
-- This database is for local testing only.
-- It does NOT connect to the real Oracle/IFS database.
-- ============================================================


-- ============================================================
-- 1. ACTIVE USERS
-- ============================================================

DROP TABLE IF EXISTS active_users;

CREATE TABLE active_users (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    full_name TEXT,
    department TEXT,
    login_time TEXT NOT NULL,
    last_activity_time TEXT,
    status TEXT NOT NULL
        CHECK(status IN ('ACTIVE','IDLE','LOGGED_OUT'))
);

INSERT INTO active_users VALUES
(1,'IFSADMIN','IFS Administrator','IT',
 '2026-10-27 08:00:00','2026-10-27 09:05:00','ACTIVE'),

(2,'SSAXENA','Sujeet Saxena','Support',
 '2026-10-27 08:10:00','2026-10-27 09:06:00','ACTIVE'),

(3,'FINUSER1','Finance User','Finance',
 '2026-10-27 08:20:00','2026-10-27 08:45:00','IDLE'),

(4,'SCMUSER1','Supply Chain User','SCM',
 '2026-10-27 08:30:00','2026-10-27 09:02:00','ACTIVE'),

(5,'OLDUSER','Logged Out User','HR',
 '2026-10-26 15:00:00','2026-10-26 17:30:00','LOGGED_OUT');


-- ============================================================
-- 2. DATABASE SESSIONS
-- ============================================================

DROP TABLE IF EXISTS database_sessions;

CREATE TABLE database_sessions (
    session_id INTEGER PRIMARY KEY,
    serial_number INTEGER,
    username TEXT NOT NULL,
    machine TEXT,
    module TEXT,
    sql_id TEXT,
    logon_time TEXT NOT NULL,
    last_call_seconds INTEGER DEFAULT 0,
    status TEXT NOT NULL
        CHECK(status IN ('ACTIVE','INACTIVE','KILLED'))
);

INSERT INTO database_sessions VALUES
(101,501,'IFSADMIN','IFSAPP01','Aurena','7abc123',
 '2026-10-27 08:00:00',5,'ACTIVE'),

(102,502,'FINUSER1','IFSAPP02','Finance','8def456',
 '2026-10-27 08:05:00',120,'ACTIVE'),

(103,503,'SCMUSER1','IFSAPP03','Order Entry','9ghi789',
 '2026-10-27 08:10:00',900,'INACTIVE'),

(104,504,'BATCHUSER','IFSAPP01','Background Job','1jkl012',
 '2026-10-27 06:00:00',15,'ACTIVE');


-- ============================================================
-- 3. DATABASE LOCKS
-- ============================================================

DROP TABLE IF EXISTS database_locks;

CREATE TABLE database_locks (
    lock_id INTEGER PRIMARY KEY,
    blocking_session_id INTEGER NOT NULL,
    blocked_session_id INTEGER NOT NULL,
    object_name TEXT,
    lock_type TEXT,
    wait_seconds INTEGER DEFAULT 0,
    created_time TEXT NOT NULL
);

INSERT INTO database_locks VALUES
(1,101,102,'CUSTOMER_ORDER_TAB','ROW LOCK',
 900,'2026-10-27 08:45:00'),

(2,104,103,'PURCHASE_ORDER_TAB','ROW LOCK',
 300,'2026-10-27 08:55:00');


-- ============================================================
-- 4. BACKGROUND JOBS
-- ============================================================

DROP TABLE IF EXISTS background_jobs;

CREATE TABLE background_jobs (
    job_id INTEGER PRIMARY KEY,
    job_name TEXT NOT NULL,
    queue_name TEXT,
    state TEXT NOT NULL
        CHECK(state IN ('POSTED','EXECUTING','READY','WARNING','ERROR')),
    progress_percent REAL DEFAULT 0,
    start_time TEXT,
    end_time TEXT,
    error_message TEXT,
    created_time TEXT,
    updated_time TEXT
);

INSERT INTO background_jobs VALUES
(1001,'Invoice Posting','FIN_QUEUE','READY',
 100,'2026-10-27 01:00:00','2026-10-27 01:03:00',NULL,
 '2026-10-27 01:00:00','2026-10-27 01:03:00'),

(1002,'Customer Sync','INT_QUEUE','ERROR',
 45,'2026-10-27 02:00:00','2026-10-27 02:01:00',
 'Customer ID missing',
 '2026-10-27 02:00:00','2026-10-27 02:01:00'),

(1003,'MRP Run','SCM_QUEUE','EXECUTING',
 60,'2026-10-27 06:00:00',NULL,NULL,
 '2026-10-27 06:00:00','2026-10-27 06:30:00'),

(1004,'Supplier Payment','FIN_QUEUE','WARNING',
 100,'2026-10-27 07:00:00','2026-10-27 07:15:00',
 'Two records skipped',
 '2026-10-27 07:00:00','2026-10-27 07:15:00'),

(1005,'Inventory Recalculation','SCM_QUEUE','POSTED',
 0,NULL,NULL,NULL,
 '2026-10-27 08:00:00','2026-10-27 08:00:00');


-- ============================================================
-- 5. APPLICATION MESSAGES
-- ============================================================

DROP TABLE IF EXISTS application_messages;

CREATE TABLE application_messages (
    message_id INTEGER PRIMARY KEY,
    message_type TEXT NOT NULL,
    source_system TEXT,
    destination_system TEXT,
    state TEXT NOT NULL
        CHECK(state IN ('WAITING','PROCESSING','SUCCESS','FAILED')),
    retry_count INTEGER DEFAULT 0,
    error_text TEXT,
    created_time TEXT NOT NULL,
    updated_time TEXT
);

INSERT INTO application_messages VALUES
(2001,'CustomerSync','CRM','IFS','FAILED',
 3,'Invalid customer number',
 '2026-10-27 08:00:00','2026-10-27 08:30:00'),

(2002,'InvoiceExport','IFS','SAP','SUCCESS',
 0,NULL,
 '2026-10-27 08:15:00','2026-10-27 08:16:00'),

(2003,'ShipmentSync','WMS','IFS','FAILED',
 2,'Connection timeout',
 '2026-10-27 08:30:00','2026-10-27 08:50:00'),

(2004,'SupplierImport','PORTAL','IFS','WAITING',
 0,NULL,
 '2026-10-27 08:45:00','2026-10-27 08:45:00'),

(2005,'OrderExport','IFS','WMS','PROCESSING',
 0,NULL,
 '2026-10-27 09:00:00','2026-10-27 09:05:00');


-- ============================================================
-- 6. PRINT JOBS
-- ============================================================

DROP TABLE IF EXISTS print_jobs;

CREATE TABLE print_jobs (
    print_job_id INTEGER PRIMARY KEY,
    report_name TEXT NOT NULL,
    printer_name TEXT,
    state TEXT NOT NULL
        CHECK(state IN ('QUEUED','PROCESSING','COMPLETED','FAILED')),
    submitted_by TEXT,
    page_count INTEGER DEFAULT 0,
    error_text TEXT,
    created_time TEXT NOT NULL,
    completed_time TEXT
);

INSERT INTO print_jobs VALUES
(3001,'Customer Invoice','PRINTER01','COMPLETED',
 'FINUSER1',3,NULL,
 '2026-10-27 07:00:00','2026-10-27 07:01:00'),

(3002,'Purchase Order','PRINTER02','FAILED',
 'SCMUSER1',0,'Printer unavailable',
 '2026-10-27 07:30:00','2026-10-27 07:31:00'),

(3003,'Picking List','PRINTER03','QUEUED',
 'WMSUSER',10,NULL,
 '2026-10-27 08:40:00',NULL);


-- ============================================================
-- 7. SCHEDULED TASKS
-- ============================================================

DROP TABLE IF EXISTS scheduled_tasks;

CREATE TABLE scheduled_tasks (
    task_id INTEGER PRIMARY KEY,
    task_name TEXT NOT NULL,
    frequency TEXT,
    last_run_time TEXT,
    next_run_time TEXT,
    last_duration_seconds INTEGER,
    task_status TEXT NOT NULL
        CHECK(task_status IN ('ACTIVE','PAUSED','FAILED'))
);

INSERT INTO scheduled_tasks VALUES
(4001,'Data Cleanup','Daily',
 '2026-10-27 01:00:00','2026-10-28 01:00:00',
 420,'ACTIVE'),

(4002,'Database Backup','Daily',
 '2026-10-27 02:00:00','2026-10-28 02:00:00',
 600,'ACTIVE'),

(4003,'Customer Sync','Hourly',
 '2026-10-27 08:00:00','2026-10-27 09:00:00',
 180,'FAILED'),

(4004,'Inventory Update','Hourly',
 '2026-10-27 08:30:00','2026-10-27 09:30:00',
 240,'ACTIVE');


-- ============================================================
-- 8. MONITORING ALERTS
-- ============================================================
-- Added to complete KPI queries 10, 11, 17 and 20.

DROP TABLE IF EXISTS monitoring_alerts;

CREATE TABLE monitoring_alerts (
    alert_id INTEGER PRIMARY KEY,
    alert_name TEXT NOT NULL,
    severity TEXT NOT NULL
        CHECK(severity IN ('INFO','WARNING','CRITICAL')),
    status TEXT NOT NULL
        CHECK(status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
    message TEXT,
    created_time TEXT NOT NULL
);

INSERT INTO monitoring_alerts VALUES
(5001,'Database Lock Detected','CRITICAL','OPEN',
 'Database session is blocked',
 '2026-10-27 08:45:00'),

(5002,'Background Job Failed','WARNING','OPEN',
 'Customer Sync job failed',
 '2026-10-27 08:30:00'),

(5003,'API Response Slow','WARNING','ACKNOWLEDGED',
 'API response time above threshold',
 '2026-10-27 08:40:00'),

(5004,'Old Alert','INFO','RESOLVED',
 'Previous monitoring alert',
 '2026-10-26 15:00:00');


-- ============================================================
-- 9. INTERFACE ERRORS
-- ============================================================
-- Added to complete KPI query 12.

DROP TABLE IF EXISTS interface_errors;

CREATE TABLE interface_errors (
    error_id INTEGER PRIMARY KEY,
    interface_name TEXT NOT NULL,
    error_message TEXT,
    acknowledged INTEGER DEFAULT 0,
    created_time TEXT NOT NULL
);

INSERT INTO interface_errors VALUES
(6001,'CustomerSync','Invalid customer number',0,
 '2026-10-27 08:30:00'),

(6002,'ShipmentSync','Connection timeout',0,
 '2026-10-27 08:50:00'),

(6003,'InvoiceExport','Temporary warning',1,
 '2026-10-27 08:20:00');


-- ============================================================
-- 10. SERVER HEALTH
-- ============================================================
-- Added to complete KPI queries 13 and 14.

DROP TABLE IF EXISTS server_health;

CREATE TABLE server_health (
    server_id INTEGER PRIMARY KEY,
    server_name TEXT NOT NULL,
    cpu_percent REAL,
    memory_percent REAL,
    disk_percent REAL,
    status TEXT
);

INSERT INTO server_health VALUES
(7001,'IFSAPP01',65,70,60,'HEALTHY'),

(7002,'IFSAPP02',88,82,75,'WARNING'),

(7003,'IFSAPP03',92,90,87,'CRITICAL');


-- ============================================================
-- 11. DATABASE HEALTH
-- ============================================================
-- Added to complete KPI query 15.

DROP TABLE IF EXISTS database_health;

CREATE TABLE database_health (
    health_id INTEGER PRIMARY KEY,
    database_name TEXT NOT NULL,
    captured_time TEXT NOT NULL,
    connection_status TEXT,
    cpu_percent REAL,
    memory_percent REAL,
    status TEXT
);

INSERT INTO database_health VALUES
(8001,'IFS_DATABASE',
 '2026-10-27 08:00:00',
 'CONNECTED',45,60,'HEALTHY'),

(8002,'IFS_DATABASE',
 '2026-10-27 09:00:00',
 'CONNECTED',55,65,'HEALTHY');


-- ============================================================
-- 12. API HEALTH
-- ============================================================
-- Added to complete KPI query 16.

DROP TABLE IF EXISTS api_health;

CREATE TABLE api_health (
    api_id INTEGER PRIMARY KEY,
    api_name TEXT NOT NULL,
    endpoint TEXT,
    http_status INTEGER,
    response_time_ms INTEGER,
    status TEXT NOT NULL
        CHECK(status IN ('UP','DOWN','DEGRADED'))
);

INSERT INTO api_health VALUES
(9001,'Customer API',
 '/api/customers',200,180,'UP'),

(9002,'Order API',
 '/api/orders',200,250,'UP'),

(9003,'Payment API',
 '/api/payments',503,5000,'DOWN'),

(9004,'Inventory API',
 '/api/inventory',200,2500,'DEGRADED');


-- ============================================================
-- DASHBOARD KPI QUERIES
-- ============================================================


-- 1. Total active users

SELECT COUNT(*) AS active_users
FROM active_users
WHERE status = 'ACTIVE';


-- 2. Active database sessions

SELECT COUNT(*) AS active_sessions
FROM database_sessions
WHERE status = 'ACTIVE';


-- 3. Background jobs in error

SELECT COUNT(*) AS jobs_in_error
FROM background_jobs
WHERE state = 'ERROR';


-- 4. Currently executing background jobs

SELECT COUNT(*) AS executing_jobs
FROM background_jobs
WHERE state = 'EXECUTING';


-- 5. Failed application messages

SELECT COUNT(*) AS failed_messages
FROM application_messages
WHERE state = 'FAILED';


-- 6. Waiting application messages

SELECT COUNT(*) AS waiting_messages
FROM application_messages
WHERE state = 'WAITING';


-- 7. Failed print jobs

SELECT COUNT(*) AS failed_print_jobs
FROM print_jobs
WHERE state = 'FAILED';


-- 8. Blocking locks

SELECT COUNT(*) AS blocking_locks
FROM database_locks;


-- 9. Longest lock wait

SELECT COALESCE(MAX(wait_seconds),0) AS longest_wait_seconds
FROM database_locks;


-- 10. Open alerts

SELECT COUNT(*) AS open_alerts
FROM monitoring_alerts
WHERE status = 'OPEN';


-- 11. Critical open alerts

SELECT COUNT(*) AS critical_open_alerts
FROM monitoring_alerts
WHERE status = 'OPEN'
AND severity = 'CRITICAL';


-- 12. Unacknowledged interface errors

SELECT COUNT(*) AS unacknowledged_errors
FROM interface_errors
WHERE acknowledged = 0;


-- 13. Server CPU and memory status

SELECT
    server_name,
    cpu_percent,
    memory_percent,
    disk_percent,
    status
FROM server_health
ORDER BY cpu_percent DESC;


-- 14. Servers above recommended threshold

SELECT
    server_name,
    cpu_percent,
    memory_percent,
    disk_percent
FROM server_health
WHERE cpu_percent >= 85
   OR memory_percent >= 85
   OR disk_percent >= 85;


-- 15. Latest database health

SELECT *
FROM database_health
ORDER BY captured_time DESC
LIMIT 1;


-- 16. APIs down or degraded

SELECT
    api_name,
    endpoint,
    http_status,
    response_time_ms,
    status
FROM api_health
WHERE status IN ('DOWN','DEGRADED');


-- 17. KPI summary in one row

SELECT
    (SELECT COUNT(*)
     FROM active_users
     WHERE status='ACTIVE') AS active_users,

    (SELECT COUNT(*)
     FROM database_sessions
     WHERE status='ACTIVE') AS active_sessions,

    (SELECT COUNT(*)
     FROM background_jobs
     WHERE state='ERROR') AS jobs_in_error,

    (SELECT COUNT(*)
     FROM application_messages
     WHERE state='FAILED') AS failed_messages,

    (SELECT COUNT(*)
     FROM print_jobs
     WHERE state='FAILED') AS failed_print_jobs,

    (SELECT COUNT(*)
     FROM database_locks) AS blocking_locks,

    (SELECT COUNT(*)
     FROM monitoring_alerts
     WHERE status='OPEN') AS open_alerts;


-- 18. Background job status chart

SELECT
    state,
    COUNT(*) AS job_count
FROM background_jobs
GROUP BY state
ORDER BY job_count DESC;


-- 19. Application message status chart

SELECT
    state,
    COUNT(*) AS message_count
FROM application_messages
GROUP BY state
ORDER BY message_count DESC;


-- 20. Alert severity chart

SELECT
    severity,
    COUNT(*) AS alert_count
FROM monitoring_alerts
WHERE status <> 'RESOLVED'
GROUP BY severity
ORDER BY alert_count DESC;