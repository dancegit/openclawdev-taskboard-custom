#!/usr/bin/env python3
"""
Migration script: Export SQLite data to PostgreSQL

NOTE: DATABASE_URL should be set via environment variable (.env file).
See .env.example for template.
"""
import sqlite3
import psycopg2
import os
from datetime import datetime
from urllib.parse import urlparse

# SQLite database path
SQLITE_DB_PATH = "data/tasks.db"

# PostgreSQL connection string (set via environment variable)
DATABASE_URL = os.getenv("DATABASE_URL",
    "postgresql://user:password@host:port/database?sslmode=require")

def parse_database_url(url):
    """Parse DATABASE_URL into components."""
    parsed = urlparse(url)
    return {
        'dbname': parsed.path[1:],
        'user': parsed.username,
        'password': parsed.password,
        'host': parsed.hostname,
        'port': parsed.port or 5432
    }

def migrate():
    """Migrate data from SQLite to PostgreSQL."""
    print("🔄 Starting migration from SQLite to PostgreSQL...")

    # Check if SQLite database exists
    if not os.path.exists(SQLITE_DB_PATH):
        print(f"⚠️  SQLite database not found at {SQLITE_DB_PATH}")
        print("This is a fresh installation - no migration needed.")
        return

    # Connect to SQLite
    print(f"📦 Connecting to SQLite: {SQLITE_DB_PATH}")
    sqlite_conn = sqlite3.connect(SQLITE_DB_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    # Connect to PostgreSQL
    print(f"🗄️  Connecting to PostgreSQL...")
    pg_params = parse_database_url(DATABASE_URL)
    pg_conn = psycopg2.connect(**pg_params)
    pg_cursor = pg_conn.cursor()
    pg_conn.autocommit = False

    try:
        # Create tables in PostgreSQL
        print("📋 Creating tables in PostgreSQL...")
        create_tables(pg_cursor)

        # Migrate tasks
        print("📋 Migrating tasks...")
        migrate_tasks(sqlite_conn, pg_cursor)

        # Migrate comments
        print("📋 Migrating comments...")
        migrate_comments(sqlite_conn, pg_cursor)

        # Migrate activity log
        print("📋 Migrating activity log...")
        migrate_activity(sqlite_conn, pg_cursor)

        # Commit all changes
        pg_conn.commit()
        print("✅ Migration completed successfully!")

        # Summary
        sqlite_cursor.execute("SELECT COUNT(*) as count FROM tasks")
        task_count = sqlite_cursor.fetchone()['count']

        print(f"\n📊 Migrated {task_count} tasks")

    except Exception as e:
        pg_conn.rollback()
        print(f"❌ Migration failed: {e}")
        raise e
    finally:
        sqlite_conn.close()
        pg_conn.close()
        print("🔌 Connections closed")

def create_tables(cursor):
    """Create tables in PostgreSQL if they don't exist."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'Backlog',
            priority TEXT DEFAULT 'Medium',
            agent TEXT DEFAULT 'Unassigned',
            due_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            board TEXT DEFAULT 'tasks',
            source_file TEXT,
            source_ref TEXT,
            agent_session_key TEXT,
            working_agent TEXT
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            agent TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            agent TEXT,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
    """)

def migrate_tasks(sqlite_conn, pg_cursor):
    """Migrate tasks from SQLite to PostgreSQL."""
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM tasks")

    rows = sqlite_cursor.fetchall()
    print(f"   Found {len(rows)} tasks")

    for row in rows:
        pg_cursor.execute("""
            INSERT INTO tasks (id, title, description, status, priority, agent,
                           due_date, created_at, updated_at, board,
                           source_file, source_ref, agent_session_key, working_agent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            row['id'], row['title'], row['description'], row['status'],
            row['priority'], row['agent'], row['due_date'],
            row['created_at'], row['updated_at'], row['board'],
            row['source_file'], row['source_ref'], row['agent_session_key'],
            row['working_agent']
        ))

def migrate_comments(sqlite_conn, pg_cursor):
    """Migrate comments from SQLite to PostgreSQL."""
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM comments")

    rows = sqlite_cursor.fetchall()
    print(f"   Found {len(rows)} comments")

    for row in rows:
        pg_cursor.execute("""
            INSERT INTO comments (id, task_id, agent, content, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (row['id'], row['task_id'], row['agent'], row['content'], row['created_at']))

def migrate_activity(sqlite_conn, pg_cursor):
    """Migrate activity log from SQLite to PostgreSQL."""
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT * FROM activity_log")

    rows = sqlite_cursor.fetchall()
    print(f"   Found {len(rows)} activity entries")

    for row in rows:
        pg_cursor.execute("""
            INSERT INTO activity_log (id, task_id, action, agent, details, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (row['id'], row['task_id'], row['action'], row['agent'], row['details'], row['created_at']))

if __name__ == "__main__":
    migrate()
