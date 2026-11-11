import sqlite3
import os
import datetime
import json
from typing import Any, Dict, List, Optional

from app.runtime_paths import WORKSPACE

DB_PATH = str(WORKSPACE / "data" / "creator_kb.db")

ACTIVE_TASK_STATUSES = ('queued', 'running', 'stopping', 'enriching')
TERMINAL_TASK_STATUSES = (
    'stopped', 'completed', 'complete', 'partial', 'blocked', 'failed', 'interrupted',
    'awaiting_selection', 'candidates_ready',
)

_CREATOR_UPSERT_FIELDS = (
    'unique_id', 'uid', 'nickname', 'avatar_url', 'followers', 'total_likes',
    'aweme_count', 'signature', 'star_atlas', 'city', 'profile_url', 'star_id',
    'price_1_20', 'price_21_60', 'median_play_30d', 'expected_cpm', 'category_tag',
    'source_platform',
)

def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # 1. 任务表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        keyword TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        created_at TEXT NOT NULL,
        requirements_json TEXT NOT NULL DEFAULT '{}',
        message TEXT NOT NULL DEFAULT '',
        progress_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """)

    # 2. 达人主档表 (一人一档)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS creators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sec_uid TEXT UNIQUE NOT NULL,
        unique_id TEXT,
        uid TEXT,
        nickname TEXT NOT NULL,
        avatar_url TEXT,
        followers INTEGER DEFAULT 0,
        total_likes INTEGER DEFAULT 0,
        aweme_count INTEGER DEFAULT 0,
        signature TEXT,
        star_atlas INTEGER DEFAULT 0,
        city TEXT,
        profile_url TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    # 3. 达人数据快照表 (每次任务采集的互动数据)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        sample_count INTEGER DEFAULT 0,
        avg_likes REAL DEFAULT 0,
        max_likes INTEGER DEFAULT 0,
        latest_post_date TEXT,
        days_since_latest INTEGER,
        captured_at TEXT NOT NULL,
        FOREIGN KEY (creator_id) REFERENCES creators(id),
        FOREIGN KEY (task_id) REFERENCES tasks(id)
    )
    """)

    # 4. 商务联系渠道表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER NOT NULL,
        contact_type TEXT NOT NULL,
        contact_value TEXT NOT NULL,
        source TEXT NOT NULL,
        verified INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY (creator_id) REFERENCES creators(id)
    )
    """)

    # 5. 样本作品表 (可解释推荐证据)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sample_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        aweme_id TEXT,
        desc TEXT,
        likes INTEGER DEFAULT 0,
        comments INTEGER DEFAULT 0,
        post_date TEXT,
        observation_id INTEGER REFERENCES snapshots(id),
        FOREIGN KEY (creator_id) REFERENCES creators(id),
        FOREIGN KEY (task_id) REFERENCES tasks(id)
    )
    """)

    # 6. AI 推荐结论表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        creator_id INTEGER NOT NULL,
        recommend_level TEXT NOT NULL,
        hard_pass INTEGER DEFAULT 1,
        reason TEXT,
        risk TEXT,
        missing_fields TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (creator_id) REFERENCES creators(id)
    )
    """)

    # 7. 系统配置与 API 接入表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    # 8. 对话历史表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        meta_json TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # 9. 候选人审核快照（每次审核保留，不覆盖旧证据）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS candidate_assessments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        creator_id INTEGER NOT NULL,
        observation_id INTEGER,
        requirements_hash TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        numeric_status TEXT NOT NULL,
        topic_status TEXT NOT NULL,
        overall_status TEXT NOT NULL,
        assessment_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (creator_id) REFERENCES creators(id)
    )
    """)
    cur.execute("""CREATE INDEX IF NOT EXISTS candidate_assessments_task_idx
                   ON candidate_assessments (task_id, creator_id, id)""")
    cur.execute("""CREATE INDEX IF NOT EXISTS candidate_assessments_creator_idx
                   ON candidate_assessments (creator_id, id)""")
    cur.execute("""CREATE INDEX IF NOT EXISTS candidate_assessments_observation_idx
                   ON candidate_assessments (observation_id)""")
    cur.execute("""CREATE INDEX IF NOT EXISTS candidate_assessments_requirements_idx
                   ON candidate_assessments (requirements_hash, policy_version)""")

    # 10. 发现来源（一个账号可以被多个查询词/来源发现，按稳定标识去重）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS creator_discovery_sources (
        task_id INTEGER NOT NULL,
        creator_id INTEGER NOT NULL,
        search_type TEXT NOT NULL,
        query TEXT NOT NULL,
        source_post_id TEXT NOT NULL DEFAULT '',
        discovered_at TEXT NOT NULL,
        UNIQUE (task_id, creator_id, search_type, query, source_post_id)
    )
    """)
    cur.execute("""CREATE INDEX IF NOT EXISTS creator_discovery_sources_task_idx
                   ON creator_discovery_sources (task_id, creator_id)""")

    # Safe migrations for Excel interactive table columns
    def add_col_if_missing(table, col, col_def):
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r["name"] for r in cur.fetchall()]
        if col not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

    add_col_if_missing("tasks", "requirements_json", "TEXT NOT NULL DEFAULT '{}'")
    add_col_if_missing("tasks", "message", "TEXT NOT NULL DEFAULT ''")
    add_col_if_missing("tasks", "progress_json", "TEXT NOT NULL DEFAULT '{}'")
    add_col_if_missing("tasks", "updated_at", "TEXT NOT NULL DEFAULT ''")
    add_col_if_missing("tasks", "started_at", "TEXT")
    add_col_if_missing("tasks", "finished_at", "TEXT")
    add_col_if_missing("tasks", "stop_requested", "INTEGER NOT NULL DEFAULT 0")
    # Correct the old presentation state without changing evidence or timing.
    for row in cur.execute("SELECT id,progress_json FROM tasks WHERE status='partial'").fetchall():
        try:
            progress = json.loads(row['progress_json'] or '{}')
        except (TypeError, ValueError):
            continue
        if isinstance(progress, dict) and progress.get('pause_reason') == 'platform_verification':
            cur.execute("UPDATE tasks SET status='blocked' WHERE id=?", (row['id'],))
    cur.execute("UPDATE tasks SET updated_at = created_at WHERE updated_at IS NULL OR updated_at = ''")
    cur.execute("""UPDATE tasks SET started_at = created_at
                   WHERE started_at IS NULL AND status != 'queued'""")
    terminal_params = ','.join('?' for _ in TERMINAL_TASK_STATUSES)
    cur.execute(f"""UPDATE tasks SET finished_at = updated_at
                    WHERE finished_at IS NULL AND status IN ({terminal_params})""", TERMINAL_TASK_STATUSES)

    add_col_if_missing("creators", "star_id", "TEXT DEFAULT ''")
    add_col_if_missing("creators", "source_platform", "TEXT NOT NULL DEFAULT 'douyin'")
    add_col_if_missing("creators", "price_1_20", "INTEGER DEFAULT 0")
    add_col_if_missing("creators", "price_21_60", "INTEGER DEFAULT 0")
    add_col_if_missing("creators", "median_play_30d", "INTEGER DEFAULT 0")
    add_col_if_missing("creators", "expected_cpm", "REAL DEFAULT 0.0")
    add_col_if_missing("creators", "category_tag", "TEXT DEFAULT ''")

    add_col_if_missing("recommendations", "bd_status", "TEXT DEFAULT '未建联'")
    add_col_if_missing("recommendations", "bd_notes", "TEXT DEFAULT ''")
    add_col_if_missing("sample_posts", "observation_id", "INTEGER REFERENCES snapshots(id)")
    cur.execute("""CREATE INDEX IF NOT EXISTS sample_posts_observation_idx
                   ON sample_posts (task_id,creator_id,observation_id,id)""")

    conn.commit()
    conn.close()

def save_task(
    name: str,
    keyword: str,
    requirements: Optional[Dict[str, Any]] = None,
    status: str = 'queued',
) -> int:
    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """INSERT INTO tasks (name, keyword, status, created_at, requirements_json, updated_at, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, keyword, status, now_str, json.dumps(requirements or {}, ensure_ascii=False), now_str,
         now_str if status != 'queued' else None,
         now_str if status in TERMINAL_TASK_STATUSES else None)
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id

def update_task_status(
    task_id: int,
    status: str,
    message: str = '',
    progress: Optional[Dict[str, Any]] = None,
):
    conn = get_connection()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    progress_json = json.dumps(progress, ensure_ascii=False) if progress is not None else None
    conn.execute("""
        UPDATE tasks
        SET status = CASE WHEN stop_requested = 1 AND ? IN ('queued', 'running', 'enriching', 'stopping')
                          THEN 'stopping' ELSE ? END,
            message = ?, progress_json = COALESCE(?, progress_json), updated_at = ?,
            started_at = CASE WHEN ? != 'queued' THEN COALESCE(started_at, ?) ELSE started_at END,
            finished_at = CASE WHEN ? THEN COALESCE(finished_at, ?)
                               WHEN ? IN ('queued', 'running', 'enriching', 'stopping') THEN NULL
                               ELSE finished_at END
        WHERE id = ?
    """, (status, status, message, progress_json, now_str, status, now_str,
          status in TERMINAL_TASK_STATUSES, now_str, status, task_id))
    conn.commit()
    conn.close()

def _task_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    task = dict(row)
    task["requirements"] = json.loads(task.pop("requirements_json") or "{}")
    task["progress"] = json.loads(task.pop("progress_json") or "{}")
    return task

def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute("""
        SELECT t.*, COUNT(DISTINCT r.creator_id) as creator_count
        FROM tasks t
        LEFT JOIN recommendations r ON t.id = r.task_id
        WHERE t.id = ?
        GROUP BY t.id
    """, (task_id,)).fetchone()
    conn.close()
    return _task_from_row(row) if row else None

def recover_interrupted_tasks():
    conn = get_connection()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE tasks
        SET status = 'interrupted', message = ?, updated_at = ?,
            finished_at = COALESCE(finished_at, ?)
        WHERE status IN ('queued', 'running', 'stopping', 'enriching')
    """, ("Task interrupted by service restart.", now_str, now_str))
    conn.commit()
    conn.close()

def upsert_creator(data: Dict[str, Any]) -> int:
    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("SELECT id, updated_at FROM creators WHERE sec_uid = ?", (data["sec_uid"],))
    row = cur.fetchone()
    if row:
        cid = row["id"]
        fields = _CREATOR_UPSERT_FIELDS
        supplied = [field for field in fields if field in data]
        # A discovery identity is not a new observation of retained follower metrics.
        updated_at = now_str if 'followers' in data else row['updated_at']
        assignments = ', '.join(f'{field} = ?' for field in supplied)
        if assignments:
            assignments += ', '
        cur.execute(f"UPDATE creators SET {assignments}updated_at = ? WHERE id = ?",
                    (*[data[field] for field in supplied], updated_at, cid))
    else:
        cur.execute("""
            INSERT INTO creators (
                sec_uid, unique_id, uid, nickname, avatar_url,
                followers, total_likes, aweme_count, signature,
                star_atlas, city, profile_url, created_at, updated_at,
                star_id, price_1_20, price_21_60, median_play_30d, expected_cpm,
                source_platform
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["sec_uid"], data.get("unique_id"), data.get("uid"),
            data.get("nickname", data["sec_uid"]), data.get("avatar_url"), data.get("followers"),
            data.get("total_likes"), data.get("aweme_count"), data.get("signature"),
            data.get("star_atlas"), data.get("city"), data.get("profile_url"),
            now_str, now_str, data.get("star_id") or None,
            data.get("price_1_20"), data.get("price_21_60"),
            data.get("median_play_30d"), data.get("expected_cpm"),
            data.get("source_platform", "douyin")
        ))
        cid = cur.lastrowid

    conn.commit()
    conn.close()
    return cid

def _creator_by_star_id(conn: sqlite3.Connection, star_id: str) -> Optional[sqlite3.Row]:
    if not isinstance(star_id, str) or not star_id.strip():
        raise ValueError('star_id must be a nonempty string')
    rows = conn.execute('SELECT * FROM creators WHERE star_id = ? LIMIT 2',
                        (star_id.strip(),)).fetchall()
    if len(rows) > 1:
        raise ValueError('Ambiguous star_id: multiple creators match')
    return rows[0] if rows else None


def get_creator_by_star_id(star_id: str) -> Optional[Dict[str, Any]]:
    """Return the complete creator row, or None; reject ambiguous legacy IDs."""
    conn = get_connection()
    try:
        row = _creator_by_star_id(conn, star_id)
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_xingtu_creator(data: Dict[str, Any]) -> int:
    """Store Xingtu identity, matching only star_id or an unambiguous actual uid.

    star_id is required; uid is the optional Douyin core_user_id, never a
    sec_uid. New sec_uid values are internal xingtu: keys, not profile IDs.
    profile_url must be a caller-verified URL; no URL or audience evidence is
    inferred. Conflicting/ambiguous identities raise ValueError without writes.
    """
    star_id = data.get('star_id')
    if not isinstance(star_id, str) or not star_id.strip():
        raise ValueError('star_id must be a nonempty string')
    star_id = star_id.strip()
    uid = data.get('uid')
    if uid is not None and not isinstance(uid, str):
        raise ValueError('uid must be an actual core_user_id string or None')
    uid = (uid.strip() or None) if uid is not None else None
    supplied = {field: data[field] for field in _CREATOR_UPSERT_FIELDS if field in data}
    supplied.update(star_id=star_id, source_platform='xingtu')
    if uid:
        supplied['uid'] = uid
    else:
        supplied.pop('uid', None)

    conn = get_connection()
    try:
        with conn:
            # Serialize identity resolution and writes, including first discovery.
            conn.execute('BEGIN IMMEDIATE')
            row = _creator_by_star_id(conn, star_id)
            if uid:
                matches = conn.execute('SELECT * FROM creators WHERE uid = ? LIMIT 2',
                                       (uid,)).fetchall()
                if len(matches) > 1:
                    raise ValueError('Ambiguous uid: multiple creators match')
                if row and row['uid'] and row['uid'] != uid:
                    raise ValueError('Conflicting uid for existing star_id')
                if matches:
                    match = matches[0]
                    if row and match['id'] != row['id']:
                        raise ValueError('star_id and uid identify different creators')
                    if match['star_id'] and match['star_id'] != star_id:
                        raise ValueError('uid is already bound to another star_id')
                    row = row or match

            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if row:
                cid = row['id']
                supplied['updated_at'] = now_str if 'followers' in data else row['updated_at']
                assignments = ', '.join(f'{field} = ?' for field in supplied)
                conn.execute(f'UPDATE creators SET {assignments} WHERE id = ?',
                             (*supplied.values(), cid))
            else:
                internal_key = 'xingtu:' + star_id
                if conn.execute('SELECT id FROM creators WHERE sec_uid = ?',
                                (internal_key,)).fetchone():
                    raise ValueError('Internal Xingtu key conflicts with existing creator')
                # Missing measurements stay unknown, rather than using schema zeros.
                values = {field: supplied.get(field) for field in _CREATOR_UPSERT_FIELDS}
                values.update(sec_uid=internal_key, created_at=now_str, updated_at=now_str)
                values['nickname'] = data.get('nickname') or star_id
                columns = ', '.join(values)
                placeholders = ', '.join('?' for _ in values)
                cursor = conn.execute(f'INSERT INTO creators ({columns}) VALUES ({placeholders})',
                                      tuple(values.values()))
                cid = cursor.lastrowid
        return cid
    finally:
        conn.close()

def save_snapshot(creator_id: int, task_id: int, snapshot: Dict[str, Any]) -> int:
    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO snapshots (
            creator_id, task_id, sample_count, avg_likes, max_likes,
            latest_post_date, days_since_latest, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        creator_id, task_id, snapshot.get("sample_count", 0),
        snapshot.get("avg_likes"), snapshot.get("max_likes"),
        snapshot.get("latest_post_date"), snapshot.get("days_since_latest"),
        now_str
    ))
    snapshot_id = cur.lastrowid
    conn.commit()
    conn.close()
    return snapshot_id

def save_contacts(creator_id: int, contacts_list: List[Dict[str, Any]]):
    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for c in contacts_list:
        cur.execute(
            "SELECT id FROM contacts WHERE creator_id = ? AND contact_value = ?",
            (creator_id, c["value"])
        )
        if not cur.fetchone():
            cur.execute("""
                INSERT INTO contacts (creator_id, contact_type, contact_value, source, verified, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (creator_id, c.get("type", "微信"), c["value"], c.get("source", "主页简介"),
                  1 if c.get("verified", False) else 0, now_str))
    conn.commit()
    conn.close()

def save_sample_posts(creator_id: int, task_id: int, posts: List[Dict[str, Any]]):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('BEGIN IMMEDIATE')
    snapshot = cur.execute('''SELECT id FROM snapshots WHERE creator_id=? AND task_id=?
                             ORDER BY id DESC LIMIT 1''', (creator_id, task_id)).fetchone()
    observation_id = snapshot['id'] if snapshot else None
    for p in posts:
        cur.execute("""
            INSERT INTO sample_posts (creator_id, task_id, aweme_id, desc, likes, comments, post_date, observation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            creator_id, task_id, p.get("aweme_id"), p.get("desc"),
            p.get("likes", 0), p.get("comments", 0), p.get("date"), observation_id
        ))
    conn.commit()
    conn.close()

def _observation_posts(conn, creator_id, task_id, observation_id, limit=-1):
    # Legacy rows have no provable association; never mix them with associated observations.
    associated = conn.execute('''SELECT 1 FROM sample_posts WHERE creator_id=? AND task_id=?
                                 AND observation_id IS NOT NULL LIMIT 1''', (creator_id, task_id)).fetchone()
    condition = 'observation_id=?' if associated else 'observation_id IS NULL'
    args = (creator_id, task_id, observation_id, limit) if associated else (creator_id, task_id, limit)
    rows = conn.execute(f'''SELECT aweme_id,desc,likes,comments,post_date,observation_id FROM sample_posts
                           WHERE creator_id=? AND task_id=? AND {condition} ORDER BY id LIMIT ?''', args).fetchall()
    return [dict(row) for row in rows], bool(rows) and not associated

def save_recommendation(creator_id: int, task_id: int, rec: Dict[str, Any]):
    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values = (
        rec.get("level", "待核实"),
        None if rec.get("hard_pass") is None else int(bool(rec["hard_pass"])),
        rec.get("reason", ""), rec.get("risk", ""), rec.get("missing_fields", ""), now_str,
    )
    # Keep historical duplicates, but replace the current discovery placeholder.
    cur.execute('BEGIN IMMEDIATE')
    row = cur.execute("""SELECT id FROM recommendations WHERE task_id = ? AND creator_id = ?
                         ORDER BY id DESC LIMIT 1""", (task_id, creator_id)).fetchone()
    if row:
        cur.execute("""UPDATE recommendations SET recommend_level=?, hard_pass=?, reason=?, risk=?,
                       missing_fields=?, created_at=? WHERE id=?""", (*values, row['id']))
    else:
        cur.execute("""
        INSERT INTO recommendations (
            task_id, creator_id, recommend_level, hard_pass, reason, risk, missing_fields, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, creator_id, *values))
    conn.commit()
    conn.close()

def get_task_results(task_id: Optional[int] = None) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()

    if task_id:
        target_task_id = task_id
    else:
        cur.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            conn.close()
            return []
        target_task_id = row["id"]

    query = """
    SELECT 
        c.id as creator_id,
        c.nickname,
        c.unique_id,
        c.sec_uid,
        c.uid,
        c.source_platform,
        c.followers,
        c.total_likes,
        c.aweme_count,
        c.signature,
        c.star_atlas,
        c.star_id,
        c.price_1_20,
        c.price_21_60,
        c.median_play_30d,
        c.expected_cpm,
        c.category_tag,
        c.avatar_url,
        c.profile_url,
        c.updated_at,
        s.id as observation_id,
        s.captured_at,
        s.sample_count,
        s.avg_likes,
        s.max_likes,
        s.latest_post_date,
        s.days_since_latest,
        r.id as rec_id,
        r.recommend_level,
        r.hard_pass,
        r.reason,
        r.risk,
        r.missing_fields,
        r.bd_status,
        r.bd_notes
    FROM recommendations r
    JOIN creators c ON r.creator_id = c.id
    LEFT JOIN snapshots s ON s.id = (
        SELECT MAX(s2.id) FROM snapshots s2 WHERE s2.creator_id = c.id AND s2.task_id = r.task_id
    )
    WHERE r.task_id = ? AND r.id = (
        SELECT MAX(r2.id) FROM recommendations r2 WHERE r2.creator_id = c.id AND r2.task_id = r.task_id
    )
    ORDER BY 
        CASE r.recommend_level
            WHEN '高推荐' THEN 1
            WHEN '中推荐' THEN 2
            WHEN '待核实' THEN 3
            ELSE 4
        END ASC,
        s.avg_likes DESC
    """
    cur.execute(query, (target_task_id,))
    rows = cur.fetchall()

    results = []
    for r in rows:
        item = dict(r)
        cid = item["creator_id"]
        cur.execute("SELECT contact_type, contact_value, source FROM contacts WHERE creator_id = ?", (cid,))
        item["contacts"] = [dict(c) for c in cur.fetchall()]

        item['sample_posts'], item['sample_posts_legacy_fallback'] = _observation_posts(
            conn, cid, target_task_id, item['observation_id'], limit=5)

        # Extract convenient wechat & phone
        wx = next((c["contact_value"] for c in item["contacts"] if c.get("contact_type") == "微信"), "")
        ph = next((c["contact_value"] for c in item["contacts"] if c.get("contact_type") in ("电话", "手机")), "")
        item["wechat"] = wx
        item["phone"] = ph
        if not item.get("bd_status"):
            item["bd_status"] = "未建联"
        if not item.get("bd_notes"):
            item["bd_notes"] = ""

        results.append(item)

    conn.close()
    return results

def get_observation_posts(creator_id: int, task_id: int, observation_id: int, limit: int = 10) -> Dict[str, Any]:
    """Return posts bound to one observation; legacy rows are explicitly marked.

    The UI may show five evidence rows, but review reads up to ten distinct
    posts from the same observation.  Legacy rows are never silently mixed in.
    """

    conn = get_connection()
    try:
        posts, legacy = _observation_posts(conn, creator_id, task_id, observation_id, limit=limit)
    finally:
        conn.close()
    return {'posts': posts, 'legacy_fallback': legacy, 'observation_id': observation_id}


def save_discovery_source(task_id: int, creator_id: int, search_type: str, query: str,
                          source_post_id: str = '') -> None:
    """Persist a recall source immediately, independent of later model success."""

    conn = get_connection()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""INSERT OR IGNORE INTO creator_discovery_sources
                    (task_id, creator_id, search_type, query, source_post_id, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                 (task_id, creator_id, str(search_type or ''), str(query or ''),
                  str(source_post_id or ''), now_str))
    conn.commit()
    conn.close()


def get_discovery_sources(task_id: int, creator_id: int) -> List[Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute("""SELECT task_id,creator_id,search_type,query,source_post_id,discovered_at
                           FROM creator_discovery_sources WHERE task_id=? AND creator_id=?
                           ORDER BY rowid""", (task_id, creator_id)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def save_candidate_assessment(task_id: int, creator_id: int, observation_id: Optional[int],
                              assessment: Dict[str, Any]) -> int:
    """Append an auditable assessment; old assessments are never overwritten."""

    assessment = dict(assessment or {})
    numeric = assessment.get('numeric') or {}
    topic = assessment.get('topic') or {}
    conn = get_connection()
    cur = conn.cursor()
    # Bind to the persisted representation (platform post timestamps/statistics
    # are lossy on storage), retaining the original review digest for audit.
    if assessment.get('input_hash') and assessment.get('policy'):
        from app import qualification
        creator, posts = _assessment_inputs(conn, task_id, creator_id, observation_id,
                                            assessment['policy']['sample_limit'])
        task = get_task(task_id)
        if creator and task:
            assessment['review_input_hash'] = assessment['input_hash']
            assessment['input_hash'] = qualification.assessment_input_hash(
                creator, posts, task['requirements'], assessment['policy'])
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur.execute("""
        INSERT INTO candidate_assessments
        (task_id, creator_id, observation_id, requirements_hash, policy_version, input_hash,
         numeric_status, topic_status, overall_status, assessment_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task_id, creator_id, observation_id,
        str(assessment.get('requirements_hash', '')),
        str(assessment.get('policy_version', '')),
        str(assessment.get('input_hash', '')),
        str(numeric.get('status') or assessment.get('numeric_status') or 'unknown'),
        str(topic.get('topic_status') or assessment.get('topic_status') or 'unknown'),
        str(assessment.get('overall_status') or 'pending'),
        json.dumps(assessment, ensure_ascii=False), now_str,
    ))
    assessment_id = cur.lastrowid
    conn.commit()
    conn.close()
    return assessment_id


def _assessment_inputs(conn, task_id, creator_id, observation_id, limit):
    row = conn.execute('''SELECT c.*,s.captured_at,s.avg_likes,s.max_likes,
                         s.days_since_latest,s.latest_post_date FROM creators c
                         LEFT JOIN snapshots s ON s.id=? AND s.task_id=? AND s.creator_id=c.id
                         WHERE c.id=?''', (observation_id, task_id, creator_id)).fetchone()
    posts, _ = _observation_posts(conn, creator_id, task_id, observation_id, limit=limit)
    return dict(row) if row else {}, posts


def candidate_assessment_reusable(assessment):
    if not assessment:
        return False
    numeric = assessment.get('numeric') or {}
    topic = assessment.get('topic') or {}
    identity = topic.get('account') or {}
    excluded = (assessment.get('overall_status') == 'rejected'
                and 'account_type_excluded' in (assessment.get('reason_codes') or []))
    return (numeric.get('status') == 'failed' or
            excluded or
            (assessment.get('topic_reviewed') is True and not topic.get('error_code')
             and topic.get('topic_status') in ('matched', 'not_matched')
             and identity.get('account_type') in ('independent_creator', 'creator_team', 'brand_store', 'institution')))


def _assessment_fresh(created_at: str) -> bool:
    try:
        stamp = datetime.datetime.fromisoformat(str(created_at))
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
        ttl = float(get_setting('dynamic_ttl_hours', '24'))
        if not (ttl > 0):
            ttl = 24.0
        age = (datetime.datetime.now().astimezone() - stamp).total_seconds()
        return 0 <= age <= ttl * 3600
    except (TypeError, ValueError, OverflowError):
        return False


def get_current_candidate_assessment(task_id: int, creator_id: int) -> Optional[Dict[str, Any]]:
    """Return only an assessment matching the current observation, rules and policy.

    Old rows stay in the database as history, but they cannot masquerade as the
    current review.  Historical candidates without a current assessment are
    handled as ``pending`` by callers, never promoted automatically.
    """

    task = get_task(task_id)
    if task is None:
        return None
    from app import qualification

    requirements = task.get('requirements') or {}
    policy = qualification.policy_from_requirements(requirements)
    prompt_version = qualification.get_qualification_prompt()['version']
    expected_requirements = qualification.requirements_hash(requirements)
    expected_policy = qualification.policy_fingerprint(policy, prompt_version)

    conn = get_connection()
    row = conn.execute("""SELECT * FROM candidate_assessments WHERE task_id=? AND creator_id=?
                          ORDER BY id DESC LIMIT 1""", (task_id, creator_id)).fetchone()
    observation = conn.execute("""SELECT MAX(id) FROM snapshots WHERE task_id=? AND creator_id=?""",
                               (task_id, creator_id)).fetchone()
    current_observation = observation[0] if observation else None
    creator, posts = _assessment_inputs(conn, task_id, creator_id, current_observation,
                                        policy['sample_limit'])
    conn.close()
    if row is None:
        return None
    if row['observation_id'] is not None and row['observation_id'] != current_observation:
        return None
    if row['observation_id'] is None and current_observation is not None:
        return None
    if row['requirements_hash'] != expected_requirements:
        return None
    if row['policy_version'] != expected_policy:
        return None
    if not _assessment_fresh(row['created_at']):
        return None
    if row['input_hash'] != qualification.assessment_input_hash(creator, posts, requirements, policy):
        return None
    numeric = qualification.evaluate_numeric_conditions(creator, requirements)
    if row['numeric_status'] != numeric['status']:
        return None
    if current_observation is not None and not _assessment_fresh(creator.get('captured_at')):
        return None
    item = dict(row)
    try:
        payload = json.loads(item.pop('assessment_json') or '{}')
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    item['assessment'] = payload
    item['numeric'] = payload.get('numeric') or {}
    item['topic'] = payload.get('topic') or {}
    item['candidate_status'] = item.get('overall_status') or 'pending'
    item['topic_reviewed'] = bool(payload.get('topic_reviewed'))
    item['sources'] = get_discovery_sources(task_id, creator_id) or payload.get('sources') or []
    return item


def save_task_result_item(task_id: int, item: Dict[str, Any]):
    """更新单个达人在任务中的推荐、档案、快照与跟进信息。"""
    conn = get_connection()
    cur = conn.cursor()
    cid = item.get("creator_id")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Update creators
    cur.execute("""
        UPDATE creators
        SET nickname = ?, unique_id = ?, followers = ?, category_tag = ?,
            star_id = ?, price_1_20 = ?, price_21_60 = ?, median_play_30d = ?,
            expected_cpm = ?, profile_url = ?, updated_at = ?
        WHERE id = ?
    """, (
        item.get("nickname", ""),
        item.get("unique_id", ""),
        int(item["followers"]) if item.get("followers") not in (None, "") else None,
        item.get("category_tag", ""),
        item.get("star_id") or None,
        int(item["price_1_20"]) if item.get("price_1_20") not in (None, "") else None,
        int(item["price_21_60"]) if item.get("price_21_60") not in (None, "") else None,
        int(item["median_play_30d"]) if item.get("median_play_30d") not in (None, "") else None,
        float(item["expected_cpm"]) if item.get("expected_cpm") not in (None, "") else None,
        item.get("profile_url", ""),
        now_str,
        cid
    ))

    # 2. Update recommendations
    cur.execute("""
        UPDATE recommendations
        SET recommend_level = ?, reason = ?, risk = ?, bd_status = ?, bd_notes = ?
        WHERE task_id = ? AND creator_id = ?
    """, (
        item.get("recommend_level", "待核实"),
        item.get("reason", ""),
        item.get("risk", ""),
        item.get("bd_status", "未建联"),
        item.get("bd_notes", ""),
        task_id,
        cid
    ))

    # 3. Update snapshots if exists
    cur.execute("""
        UPDATE snapshots
        SET avg_likes = ?, max_likes = ?, latest_post_date = ?, days_since_latest = ?
        WHERE task_id = ? AND creator_id = ?
    """, (
        float(item["avg_likes"]) if item.get("avg_likes") not in (None, "") else None,
        int(item["max_likes"]) if item.get("max_likes") not in (None, "") else None,
        item.get("latest_post_date", ""),
        int(item.get("days_since_latest", 0) or 0) if item.get("days_since_latest") is not None else None,
        task_id,
        cid
    ))

    # 4. Update or Insert contacts (微信 / 电话)
    wechat = str(item.get("wechat") or "").strip()
    if wechat:
        cur.execute("SELECT id FROM contacts WHERE creator_id = ? AND contact_type = '微信'", (cid,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE contacts SET contact_value = ? WHERE id = ?", (wechat, row["id"]))
        else:
            cur.execute("INSERT INTO contacts (creator_id, contact_type, contact_value, source, verified, created_at) VALUES (?, '微信', ?, '手动录入', 1, ?)", (cid, wechat, now_str))
    elif "wechat" in item:
        cur.execute("DELETE FROM contacts WHERE creator_id = ? AND contact_type = '微信'", (cid,))

    phone = str(item.get("phone") or "").strip()
    if phone:
        cur.execute("SELECT id FROM contacts WHERE creator_id = ? AND contact_type = '电话'", (cid,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE contacts SET contact_value = ? WHERE id = ?", (phone, row["id"]))
        else:
            cur.execute("INSERT INTO contacts (creator_id, contact_type, contact_value, source, verified, created_at) VALUES (?, '电话', ?, '手动录入', 1, ?)", (cid, phone, now_str))
    elif "phone" in item:
        cur.execute("DELETE FROM contacts WHERE creator_id = ? AND contact_type IN ('电话', '手机')", (cid,))

    conn.commit()
    conn.close()

def batch_save_task_results(task_id: int, items: List[Dict[str, Any]]):
    """批量更新任务结果表格数据。"""
    for item in items:
        save_task_result_item(task_id, item)

def add_empty_creator_row(task_id: int) -> Dict[str, Any]:
    """在当前任务中增补一行空白达人，供用户手动填写。"""
    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import uuid
    dummy_sec_uid = f"manual_{uuid.uuid4().hex[:12]}"
    cur.execute("""
        INSERT INTO creators (
            sec_uid, unique_id, nickname, followers, created_at, updated_at,
            star_atlas, star_id, price_1_20, price_21_60, median_play_30d, expected_cpm
        )
        VALUES (?, '待补充', '新录入达人', NULL, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL)
    """, (dummy_sec_uid, now_str, now_str))
    cid = cur.lastrowid

    cur.execute("""
        INSERT INTO snapshots (creator_id, task_id, sample_count, avg_likes, max_likes, captured_at)
        VALUES (?, ?, 0, 0, 0, ?)
    """, (cid, task_id, now_str))

    cur.execute("""
        INSERT INTO recommendations (task_id, creator_id, recommend_level, hard_pass, reason, risk, bd_status, bd_notes, created_at)
        VALUES (?, ?, '待核实', NULL, '手动增补达人条目', '', '未建联', '', ?)
    """, (task_id, cid, now_str))

    conn.commit()
    conn.close()
    return {"creator_id": cid, "sec_uid": dummy_sec_uid}

def delete_creator_from_task(task_id: int, creator_id: int):
    """从任务推荐报告中移除达人。"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM recommendations WHERE task_id = ? AND creator_id = ?", (task_id, creator_id))
    cur.execute("DELETE FROM snapshots WHERE task_id = ? AND creator_id = ?", (task_id, creator_id))
    conn.commit()
    conn.close()

def get_all_tasks() -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.*, COUNT(DISTINCT r.creator_id) as creator_count
        FROM tasks t
        LEFT JOIN recommendations r ON t.id = r.task_id
        GROUP BY t.id
        ORDER BY t.id DESC
    """)
    rows = [_task_from_row(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_knowledge_base(search: str = "") -> List[Dict[str, Any]]:
    """获取达人知识库全量档案（一人一档，带最新数据快照与联系方式）。"""
    conn = get_connection()
    cur = conn.cursor()

    query = """
    SELECT 
        c.id as creator_id,
        c.nickname,
        c.unique_id,
        c.sec_uid,
        c.uid,
        c.source_platform,
        c.star_id,
        c.followers,
        c.total_likes,
        c.aweme_count,
        c.signature,
        c.star_atlas,
        c.avatar_url,
        c.profile_url,
        c.created_at as first_seen,
        c.updated_at,
        (SELECT s.avg_likes FROM snapshots s WHERE s.creator_id = c.id ORDER BY s.id DESC LIMIT 1) as latest_avg_likes,
        (SELECT s.max_likes FROM snapshots s WHERE s.creator_id = c.id ORDER BY s.id DESC LIMIT 1) as latest_max_likes,
        (SELECT s.latest_post_date FROM snapshots s WHERE s.creator_id = c.id ORDER BY s.id DESC LIMIT 1) as latest_post_date,
        (SELECT s.days_since_latest FROM snapshots s WHERE s.creator_id = c.id ORDER BY s.id DESC LIMIT 1) as days_since_latest,
        (SELECT r.recommend_level FROM recommendations r WHERE r.creator_id = c.id ORDER BY r.id DESC LIMIT 1) as last_recommend_level
    FROM creators c
    """
    params = []
    if search:
        query += " WHERE c.nickname LIKE ? OR c.unique_id LIKE ? OR c.signature LIKE ?"
        like_str = f"%{search}%"
        params.extend([like_str, like_str, like_str])

    query += " ORDER BY c.followers DESC"
    cur.execute(query, params)
    rows = cur.fetchall()

    creators = []
    for r in rows:
        item = dict(r)
        cid = item["creator_id"]
        cur.execute("SELECT contact_type, contact_value, source FROM contacts WHERE creator_id = ?", (cid,))
        item["contacts"] = [dict(c) for c in cur.fetchall()]
        creators.append(item)

    conn.close()
    return creators

def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def save_setting(key: str, value: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_all_settings() -> Dict[str, str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    rows = cur.fetchall()
    conn.close()
    defaults = {
        "llm_enabled": "false",
        "llm_provider": "DeepSeek",
        "llm_api_key": "",
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_model": "deepseek-chat",
        "dormant_days_limit": "60",
        "active_days_limit": "14",
        "default_sample_count": "5",
        "single_post_sample": "10",
        "dynamic_ttl_hours": "24"
    }
    for r in rows:
        defaults[r["key"]] = r["value"]
    return defaults

def save_chat_message(role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> int:
    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta_str = json.dumps(meta, ensure_ascii=False) if meta else None
    cur.execute(
        "INSERT INTO chat_messages (role, content, meta_json, created_at) VALUES (?, ?, ?, ?)",
        (role, content, meta_str, now_str)
    )
    message_id = cur.lastrowid
    conn.commit()
    conn.close()
    return message_id

def clear_chat_history():
    conn = get_connection()
    conn.execute("DELETE FROM chat_messages")
    conn.commit()
    conn.close()

def get_chat_history(limit: int = 30) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, role, content, meta_json, created_at
        FROM (
            SELECT id, role, content, meta_json, created_at
            FROM chat_messages ORDER BY id DESC LIMIT ?
        )
        ORDER BY id ASC
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    res = []
    for r in rows:
        item = dict(r)
        if item["meta_json"]:
            try:
                item["meta"] = json.loads(item["meta_json"])
            except Exception:
                item["meta"] = None
        else:
            item["meta"] = None
        res.append(item)
    return res
