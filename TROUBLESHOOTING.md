# OpenClaw Agent Spawning - Troubleshooting Guide

## Problem: Agent spawning from Docker container fails

When trying to spawn OpenClaw agents from the Docker taskboard container, the spawn requests fail silently with no error messages.

## Root Causes & Fixes

### Issue 1: UFW Firewall Blocking Docker Network Access

**Symptoms:**
- Container can't reach OpenClaw gateway on port 18789
- HTTP POST requests timeout or get silently dropped
- Gateway shows no incoming requests from container

**Root Cause:**
OpenClaw gateway listens on `0.0.0.0:18789` (all interfaces), but UFW firewall has no rule allowing traffic to port 18789 from the Docker network (10.0.0.0/8).

**Fix:**
Add UFW rule to allow Docker network access to gateway port:

```bash
# Allow Docker network (10.0.0.0/8) to access OpenClaw gateway on port 18789
sudo ufw allow from 10.0.0.0/8 to any port 18789 comment 'OpenClaw Gateway Docker'

# Verify rule added
sudo ufw status numbered | grep 18789
```

**Verification:**
```bash
# Test connectivity from container
docker exec moltdev-taskboard python -c "
import httpx
import asyncio

async def test():
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get('http://10.0.0.1:18789/api/v1/health')
        print(f'Status: {r.status_code}')

asyncio.run(test())
"
```

---

### Issue 2: Phantom Tasks (Tasks stuck in In Progress with no valid session)

**Symptoms:**
- Tasks stuck in "In Progress" for >2 hours with no valid session key
- `working_agent` field has agent name but `agent_session_key` is `NULL`
- Tasks never automatically transition out of "In Progress"
- Agent thinking indicators stuck indefinitely

**Root Cause:**
Multiple issues contribute to phantom tasks:
1. `start_work()` function unconditionally overwrites `agent_session_key` when called
2. Tasks may have been assigned agents but never spawned
3. Database state corruption from failed spawn attempts

**Fix:**
Clear phantom tasks and reset to Backlog:

```bash
# Run cleanup script from container
cd /home/clauderun/openclawdev-taskboard

# Create cleanup script
cat > /tmp/fix_phantom_tasks.py << 'EOF'
#!/usr/bin/env python3
import sqlite3
from datetime import datetime

conn = sqlite3.connect('data/tasks.db')
cur = conn.cursor()

# Fix phantom tasks: tasks in In Progress with no valid session
phantom_tasks = [9, 12, 13]
for task_id in phantom_tasks:
    cur.execute("""
        UPDATE tasks
        SET status = 'Backlog', working_agent = NULL, agent_session_key = NULL
        WHERE id = ?
    """, (task_id,))
    print(f"✓ Reset task #{task_id} to Backlog (phantom task)")

# Fix tasks that have working_agent set but are not in In Progress
backlog_with_agent_tasks = [17, 18, 21, 24, 25, 26, 27, 30]
for task_id in backlog_with_agent_tasks:
    cur.execute("""
        UPDATE tasks
        SET working_agent = NULL, agent_session_key = NULL
        WHERE id = ? AND status != 'In Progress'
    """, (task_id,))
    print(f"✓ Cleared working_agent from task #{task_id} (not in In Progress)")

conn.commit()
conn.close()
print("\n✓ Database cleanup complete!")
EOF

# Copy to container and run
docker cp /tmp/fix_phantom_tasks.py moltdev-taskboard:/tmp/
docker exec moltdev-taskboard python3 /tmp/fix_phantom_tasks.py
```

**Verification:**
```bash
# Check for remaining phantom tasks
docker exec moltdev-taskboard python -c "
import sqlite3
conn = sqlite3.connect('/app/data/tasks.db')
cursor = conn.cursor()
cursor.execute('''
    SELECT id, title, working_agent, agent_session_key 
    FROM tasks 
    WHERE status = 'In Progress' AND (agent_session_key IS NULL OR agent_session_key = '')
''')
rows = cursor.fetchall()
if rows:
    for r in rows:
        print(f'⚠️  Phantom Task #{r[0]}: {r[1][:50]}... - Agent: {r[2]}, Session: {r[3]}')
else:
    print('✅ No phantom tasks found')
conn.close()
"
```

---

### Issue 3: Incorrect Gateway URL in Container Environment

**Symptoms:**
- Spawn requests go to wrong IP address
- Gateway logs show no incoming requests
- Container network traffic circular references

**Root Cause:**
Container's `OPENCLAW_GATEWAY_URL` environment variable is set to `http://host.docker.internal:18789`.

The `host.docker.internal` hostname resolves to Docker's internal gateway IP (172.20.0.1), which creates a circular reference - the container is trying to reach itself instead of the host's OpenClaw gateway.

**Fix:**
Override the environment variable in `docker-compose.yml` to use the Docker bridge gateway IP:

```yaml
services:
  taskboard:
    build: .
    container_name: moltdev-taskboard
    restart: unless-stopped
    ports:
      - "127.0.0.1:18080:8080"
    volumes:
      - ./data:/app/data
      - ./static:/app/static
    env_file:
      - .env
    environment:
      - PYTHONUNBUFFERED=1
      - OPENCLAW_GATEWAY_URL=http://10.0.0.1:18789  # Override .env value
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

**Why `10.0.0.1`:**
- Docker bridge network default gateway IP
- Container can reach host services via this IP
- Resolves to host machine where OpenClaw gateway is running

**Verification:**
```bash
# Check environment variable in container
docker exec moltdev-taskboard env | grep OPENCLAW_GATEWAY_URL
# Should show: OPENCLAW_GATEWAY_URL=http://10.0.0.1:18789

# Test DNS resolution
docker exec moltdev-taskboard cat /etc/hosts
docker exec moltdev-taskboard nslookup 10.0.0.1
```

---

### Issue 3: Response Parsing Bug in Spawn Functions

**Symptoms:**
- Gateway accepts spawn request (200 OK response)
- Agent session is created on gateway
- Session key is always `null` in taskboard database
- Logs show `Session: unknown` in spawn notification

**Root Cause:**
OpenClaw gateway's `/tools/invoke` API returns a nested response structure:

```json
{
  "ok": true,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\n  \"status\": \"accepted\",\n  \"childSessionKey\": \"agent:architect:subagent:...\",\n  \"runId\": \"...\"\n}"
      }
    ],
    "details": {
      "status": "accepted",
      "childSessionKey": "agent:architect:subagent:...",
      "runId": "..."
    }
  }
}
```

The code was parsing from the wrong level:
```python
# WRONG - parsing from top level of result
spawn_info = result.get("result", {})
run_id = spawn_info.get("runId", "unknown")
session_key = spawn_info.get("childSessionKey", None)  # Always None!
```

**Fix:**
Update both spawn functions in `app.py` to parse from the nested `details` object:

**Function 1: `spawn_mentioned_agent()` (line ~395-397)** - for @mentions
**Function 2: `spawn_agent_session()` (line ~615-620)** - for auto-spawning

```python
# CORRECT - parsing from nested details object
result = response.json() if response.status_code == 200 else None
if result and result.get("ok"):
    spawn_info = result.get("result", {})
    # runId and childSessionKey are nested in details
    details = spawn_info.get("details", {})
    run_id = details.get("runId", "unknown")
    session_key = details.get("childSessionKey", None)

    # Save session key to database
    if session_key:
        set_task_session(task_id, session_key)
```

**Verification:**
```bash
# Rebuild container to apply code changes
cd /home/clauderun/openclawdev-taskboard
docker-compose down
docker-compose up -d --build

# Trigger a spawn
curl -X POST "http://localhost:18080/api/tasks/14/move" \
  -H "Content-Type: application/json" \
  -d '{"status": "In Progress"}'

# Check database for session key
docker exec moltdev-taskboard python -c "
import sqlite3
conn = sqlite3.connect('/app/data/tasks.db')
cursor = conn.cursor()
cursor.execute('SELECT id, agent_session_key FROM tasks WHERE id = 14')
print(cursor.fetchone())
conn.close()
"
# Should show: (14, 'agent:architect:subagent:...')
```

---

## Complete Fix Application

To apply all fixes at once:

### 1. Add UFW Rule (one-time system setup)
```bash
sudo ufw allow from 10.0.0.0/8 to any port 18789 comment 'OpenClaw Gateway Docker'
```

### 2. Update docker-compose.yml
```bash
cd /home/clauderun/openclawdev-taskboard

# Backup current config
cp docker-compose.yml docker-compose.yml.backup

# Update docker-compose.yml with environment override
cat > docker-compose.yml << 'EOF'
services:
  taskboard:
    build: .
    container_name: moltdev-taskboard
    restart: unless-stopped
    ports:
      - "127.0.0.1:18080:8080"
    volumes:
      - ./data:/app/data
      - ./static:/app/static
    env_file:
      - .env
    environment:
      - PYTHONUNBUFFERED=1
      - OPENCLAW_GATEWAY_URL=http://10.0.0.1:18789
    extra_hosts:
      - "host.docker.internal:host-gateway"
EOF
```

### 3. Rebuild Container
```bash
# Stop and rebuild
docker-compose down
docker-compose up -d --build

# Verify environment
docker exec moltdev-taskboard env | grep OPENCLAW_GATEWAY_URL
```

### 4. Verify Fixes
```bash
# Test network connectivity
docker exec moltdev-taskboard python -c "
import httpx
import asyncio

async def test():
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get('http://10.0.0.1:18789/api/v1/health')
        print(f'✅ Gateway reachable: {r.status_code == 200}')

asyncio.run(test())
"

# Test agent spawning
curl -X POST "http://localhost:18080/api/tasks/1/comments" \
  -H "Content-Type: application/json" \
  -d '{"agent": "User", "content": "@Architect test spawn"}'

# Check logs
docker logs moltdev-taskboard --tail 20 | grep -E "(✅|🔗|Spawned)"
```

---

## Debugging Tips

### Enable Detailed Logging

The spawn functions now include debug output. Monitor logs with:

```bash
# Watch spawn attempts
docker logs moltdev-taskboard -f | grep -E "(🚀|🔗|✅)"

# Check for errors
docker logs moltdev-taskboard -f | grep "❌"

# Check gateway logs
journalctl --user -u openclaw-gateway -f
```

### Common Issues

**Issue: "Failed to spawn" with empty error message**
- Check UFW rules: `sudo ufw status | grep 18789`
- Check gateway URL: `docker exec moltdev-taskboard env | grep OPENCLAW_GATEWAY_URL`
- Test connectivity from container: `docker exec moltdev-taskboard python -c "import httpx; import asyncio; asyncio.run(httpx.AsyncClient().get('http://10.0.0.1:18789/api/v1/health'))"`

**Issue: Session key is null in database**
- Check if code changes were rebuilt: `docker images | grep taskboard`
- Verify response parsing: Check logs for `🔗 Response text:`
- Check if session was actually saved: Look for `🔒 set_task_session` in logs

**Issue: Gateway shows no incoming requests**
- Verify container IP: `docker inspect moltdev-taskboard | grep IPAddress`
- Verify gateway binding: `sudo netstat -tlnp | grep 18789`
- Check firewall: `sudo ufw status | grep 18789`

**Issue: API returns null for status/session_key after spawn**
- Wait 1-2 seconds before checking API response
- Database writes may not be immediately visible
- Check database directly for authoritative data: `docker exec moltdev-taskboard python -c "import sqlite3; conn = sqlite3.connect('/app/data/tasks.db'); cursor = conn.cursor(); cursor.execute('SELECT id, status, agent_session_key FROM tasks WHERE id = 14'); print(cursor.fetchone()); conn.close()"`

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Network (10.0.6.0/24)       │
│  ┌─────────────────┐  ┌──────────────────┐  │
│  │   Taskboard    │  │   Gateway       │  │
│  │   10.0.6.2     │  │  10.0.0.1     │  │
│  │   Container      │  │   Host Process  │  │
│  │                 │  │                 │  │
│  │  ┌───────────┐  │  ┌─────────────┐ │  │
│  │  │ FastAPI  │  │  │ OpenClaw    │ │  │
│  │  │ Backend  │  │  │ Gateway     │ │  │
│  │  └───────────┘  │  └─────────────┘ │  │
│  │       ↓ HTTP POST│  │       ↓ Spawn   │  │
│  │  /tools/invoke  │  │  Agent Session │  │
│  └─────────────────┘  └──────────────────┘  │
│                                              │
│  Port 18789 allowed via UFW rule              │
└─────────────────────────────────────────────────────┘
```

## Related Documentation

- [OPENCLAW_SETUP.md](./OPENCLAW_SETUP.md) - Complete OpenClaw integration guide
- [README.md](./README.md) - Project overview and features
- [CHANGELOG.md](./CHANGELOG.md) - Version history and updates

## Support

For issues or questions:
- Check OpenClaw Discord: https://discord.com/invite/clawd
- Review gateway logs: `journalctl --user -u openclaw-gateway -f`
- Review taskboard logs: `docker logs moltdev-taskboard -f`

**Issue: Agent names with spaces fail to spawn**
- **Root Cause**: Python dictionary keys in `AGENT_TO_OPENCLAW_ID` (app.py lines 68-71) contain spaces (e.g., `"Security Auditor"`, `"Code Reviewer"`, `"UX Manager"`)
- **Symptom**: When `task.agent` is `"UX Manager"`, the lookup `AGENT_TO_OPENCLAW_ID.get("UX Manager")` returns `None` because the key has a space and Python's `.get()` requires an exact match
- **Why it fails**: Database stores agent names WITHOUT spaces (e.g., `"Security-Auditor"`), but the dictionary keys had spaces, causing a mismatch
- **Impact**: Spawning agents with spaces in their names returns `None` for `agent_id`, causing the spawn function to exit early with `return None`

**Fix Applied** (app.py lines 68-71):
Changed dictionary keys from spaces to hyphens to match database values:
```python
# Changed FROM:
#     "Security Auditor": "security-auditor",  # ❌ Key has space!
#     "Code Reviewer": "code-reviewer",       # ❌ Key has space!
#     "UX Manager": "ux-manager",            # ❌ Key has space!

# TO:
    "Security-Auditor": "security-auditor",  # ✅ No space
    "Code-Reviewer": "code-reviewer",       # ✅ No space
    "UX-Manager": "ux-manager",            # ✅ No space
```

**Verification**:
```bash
# Restart taskboard to apply fix
cd /home/clauderun/openclawdev-taskboard
docker-compose restart

# Test agent spawning with space-containing agent names
curl -X POST "http://localhost:18080/api/tasks/1/comments" \
  -H "Content-Type: application/json" \
  -d '{"agent": "User", "content": "@UX-Manager please start working"}'

# Expected result: Agent spawns successfully (not "Failed to spawn" error)
```
