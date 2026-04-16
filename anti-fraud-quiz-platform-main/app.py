from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort, Response
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
import sqlite3
import secrets
import os
import csv
import io

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'instance', 'quiz_engine.db')
BRANCHES = ['CSE', 'ISE', 'ECE', 'EEE', 'MECH', 'CIVIL', 'AIML', 'DS', 'MBA', 'MCA', 'OTHER']
SEMS = ['1', '2', '3', '4', '5', '6', '7', '8']

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['MAX_VIOLATIONS'] = 3


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def column_exists(conn, table, column):
    cols = [row['name'] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    return column in cols


def parse_dt(value):
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def now_dt():
    return datetime.now()


def now_ts():
    return now_dt().strftime('%Y-%m-%d %H:%M:%S')


def dt_local_value(value):
    dt = parse_dt(value)
    return dt.strftime('%Y-%m-%dT%H:%M') if dt else ''


def human_dt(value):
    dt = parse_dt(value)
    return dt.strftime('%d %b %Y, %I:%M %p') if dt else '—'


def csv_to_list(value):
    if not value:
        return []
    return [item.strip() for item in str(value).split(',') if item.strip()]


def list_to_csv(items):
    cleaned = []
    for item in items or []:
        item = str(item).strip().upper()
        if item and item not in cleaned:
            cleaned.append(item)
    return ','.join(cleaned)


def target_label(branches_csv, sems_csv):
    branches = csv_to_list(branches_csv)
    sems = csv_to_list(sems_csv)
    b = 'All Branches' if 'ALL' in branches or not branches else ', '.join(branches)
    s = 'All Sems' if 'ALL' in sems or not sems else 'Sem ' + ', '.join(sems)
    return f'{b} | {s}'


def has_target(branches_csv, sems_csv, branch, sem):
    branches = csv_to_list(branches_csv)
    sems = csv_to_list(sems_csv)
    branch_ok = ('ALL' in branches or not branches or branch in branches)
    sem_ok = ('ALL' in sems or not sems or str(sem) in sems)
    return branch_ok and sem_ok


def split_multiselect(name, allowed, default='ALL'):
    values = [v.strip().upper() for v in request.form.getlist(name) if v.strip()]
    values = [v for v in values if v in allowed or v == 'ALL']
    if not values:
        values = [default]
    if 'ALL' in values:
        return ['ALL']
    return sorted(dict.fromkeys(values), key=values.index)


def selected_branch(default='ALL'):
    return (request.args.get('branch') or default).strip().upper()


def selected_sem(default='ALL'):
    return (request.args.get('sem') or default).strip().upper()


def current_user():
    if 'user_id' not in session:
        return None
    with get_db() as conn:
        return conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()


def login_required(role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if 'user_id' not in session:
                flash('Please log in first.', 'warning')
                return redirect(url_for('index'))
            if role and session.get('role') != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def admin_or_teacher_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get('role') not in {'admin', 'teacher'}:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def dashboard_name():
    return f"{session.get('role')}_dashboard"


def get_test_for_manager(conn, test_id):
    if session.get('role') == 'admin':
        return conn.execute(
            '''SELECT t.*, u.full_name AS teacher_name, u.username AS teacher_username
               FROM tests t JOIN users u ON u.id=t.teacher_id WHERE t.id=?''',
            (test_id,)
        ).fetchone()
    return conn.execute(
        '''SELECT t.*, u.full_name AS teacher_name, u.username AS teacher_username
           FROM tests t JOIN users u ON u.id=t.teacher_id WHERE t.id=? AND t.teacher_id=?''',
        (test_id, session['user_id'])
    ).fetchone()


def build_reset_token(user_id):
    token = secrets.token_urlsafe(24)
    expires = (now_dt() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        conn.execute('DELETE FROM password_resets WHERE user_id=?', (user_id,))
        conn.execute('INSERT INTO password_resets (user_id, token, expires_at) VALUES (?, ?, ?)', (user_id, token, expires))
        conn.commit()
    return token


def init_db():
    os.makedirs(os.path.join(BASE_DIR, 'instance'), exist_ok=True)
    with get_db() as conn:
        conn.executescript(open(os.path.join(BASE_DIR, 'database.sql'), 'r', encoding='utf-8').read())

        migrations = [
            ('users', 'branch', "ALTER TABLE users ADD COLUMN branch TEXT DEFAULT 'GENERAL'"),
            ('users', 'sem', "ALTER TABLE users ADD COLUMN sem TEXT DEFAULT '1'"),
            ('users', 'is_rejected', "ALTER TABLE users ADD COLUMN is_rejected INTEGER DEFAULT 0"),
            ('tests', 'target_branches', "ALTER TABLE tests ADD COLUMN target_branches TEXT DEFAULT 'ALL'"),
            ('tests', 'target_sems', "ALTER TABLE tests ADD COLUMN target_sems TEXT DEFAULT 'ALL'"),
            ('tests', 'end_at', "ALTER TABLE tests ADD COLUMN end_at TEXT"),
        ]
        for table, col, sql in migrations:
            if not column_exists(conn, table, col):
                conn.execute(sql)

        if column_exists(conn, 'tests', 'branch'):
            conn.execute("UPDATE tests SET target_branches = COALESCE(NULLIF(target_branches,''), COALESCE(NULLIF(branch,''), 'ALL'))")
        conn.execute("UPDATE tests SET target_sems = COALESCE(NULLIF(target_sems,''), 'ALL')")
        conn.execute("UPDATE tests SET end_at = COALESCE(NULLIF(end_at,''), datetime(replace(scheduled_at,'T',' '), '+' || duration_minutes || ' minutes'))")
        conn.execute("UPDATE tests SET answer_release_at = COALESCE(NULLIF(answer_release_at,''), end_at)")
        conn.execute("UPDATE users SET sem = COALESCE(NULLIF(sem,''), '1') WHERE role='student'")
        conn.execute("UPDATE users SET sem = COALESCE(NULLIF(sem,''), 'NA') WHERE role IN ('teacher','admin')")

        admin = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
        if not admin:
            conn.execute(
                '''INSERT INTO users (username, email, password_hash, full_name, role, branch, sem, is_approved, credentials)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                ('admin', 'admin@quiz.local', generate_password_hash('Admin@123'), 'System Admin', 'admin', 'ALL', 'NA', 1, 'Default system admin')
            )
        teacher = conn.execute("SELECT id FROM users WHERE username='teacher1'").fetchone()
        if not teacher:
            conn.execute(
                '''INSERT INTO users (username, email, password_hash, full_name, role, branch, sem, is_approved, credentials)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                ('teacher1', 'teacher1@quiz.local', generate_password_hash('Teacher@123'), 'Teacher One', 'teacher', 'CSE', 'NA', 1, 'MSc, 5 years experience')
            )
        student = conn.execute("SELECT id FROM users WHERE username='student1'").fetchone()
        if not student:
            conn.execute(
                '''INSERT INTO users (username, email, password_hash, full_name, role, branch, sem, is_approved, credentials)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                ('student1', 'student1@quiz.local', generate_password_hash('Student@123'), 'Student One', 'student', 'CSE', '5', 1, '')
            )

        test_exists = conn.execute('SELECT id FROM tests LIMIT 1').fetchone()
        teacher_id = conn.execute("SELECT id FROM users WHERE username='teacher1'").fetchone()['id']
        if not test_exists:
            start = now_dt() - timedelta(minutes=10)
            end = now_dt() + timedelta(hours=1)
            conn.execute(
                '''INSERT INTO tests
                   (teacher_id, title, description, scheduled_at, duration_minutes, total_marks, answer_release_at, is_active, target_branches, target_sems, end_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    teacher_id,
                    'Programming Basics Demo Test',
                    'A sample test to verify the anti-fraud platform.',
                    start.strftime('%Y-%m-%dT%H:%M'),
                    20,
                    5,
                    end.strftime('%Y-%m-%dT%H:%M'),
                    1,
                    'CSE,ISE',
                    '5,6',
                    end.strftime('%Y-%m-%dT%H:%M')
                )
            )
            test_id = conn.execute('SELECT id FROM tests ORDER BY id DESC LIMIT 1').fetchone()['id']
            sample_questions = [
                ('What does HTML stand for?', 'Hyper Text Markup Language', 'High Transfer Machine Language', 'Home Tool Markup Language', 'Hyperlink Transfer Markup Logic', 'A', 1),
                ('Which language runs in the browser?', 'Python', 'C', 'JavaScript', 'Java', 'C', 1),
                ('Which SQL command is used to read data?', 'READ', 'SELECT', 'GET', 'FETCHROW', 'B', 1),
                ('Which Flask folder stores HTML pages?', 'views', 'static', 'templates', 'public', 'C', 1),
                ('Which of these is a CSS property?', 'color', 'print', 'server', 'route', 'A', 1),
            ]
            for q in sample_questions:
                conn.execute(
                    '''INSERT INTO questions (test_id, question_text, option_a, option_b, option_c, option_d, correct_answer, marks)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (test_id, *q)
                )
        conn.commit()


app.jinja_env.globals.update(
    human_dt=human_dt,
    dt_local_value=dt_local_value,
    BRANCHES=BRANCHES,
    SEMS=SEMS,
    target_label=target_label,
    csv_to_list=csv_to_list,
)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/signup/<role>', methods=['GET', 'POST'])
def signup(role):
    if role not in {'student', 'teacher'}:
        abort(404)
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        full_name = request.form.get('full_name', '').strip()
        credentials = request.form.get('credentials', '').strip()
        branch = request.form.get('branch', 'GENERAL').strip().upper() or 'GENERAL'
        sem = request.form.get('sem', '1').strip() if role == 'student' else 'NA'
        if not username or not email or not password:
            flash('Username, email, and password are required.', 'danger')
            return redirect(request.url)
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return redirect(request.url)
        with get_db() as conn:
            existing = conn.execute('SELECT id FROM users WHERE username=? OR email=?', (username, email)).fetchone()
            if existing:
                flash('Username or email already exists.', 'danger')
                return redirect(request.url)
            conn.execute(
                '''INSERT INTO users (username, email, password_hash, full_name, role, branch, sem, credentials, is_approved)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (username, email, generate_password_hash(password), full_name, role, branch, sem, credentials, 0 if role == 'teacher' else 1)
            )
            conn.commit()
        flash('Teacher application submitted. Wait for admin approval.' if role == 'teacher' else 'Student account created. Please log in.', 'success')
        return redirect(url_for('login', role=role))
    return render_template('signup.html', role=role)


@app.route('/login/<role>', methods=['GET', 'POST'])
def login(role):
    if role not in {'student', 'teacher', 'admin'}:
        abort(404)
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        with get_db() as conn:
            user = conn.execute('SELECT * FROM users WHERE username=? AND role=?', (username, role)).fetchone()
            if not user or not check_password_hash(user['password_hash'], password):
                flash('Invalid credentials.', 'danger')
                return redirect(request.url)
            if role == 'teacher' and user['is_rejected']:
                flash('This teacher account is rejected or disabled by admin.', 'danger')
                return redirect(request.url)
            if role == 'teacher' and not user['is_approved']:
                flash('Teacher login is pending admin approval.', 'warning')
                return redirect(request.url)
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['branch'] = user['branch']
            session['sem'] = user['sem']
            conn.execute('UPDATE users SET last_login=? WHERE id=?', (now_ts(), user['id']))
            conn.commit()
        flash('Login successful.', 'success')
        return redirect(url_for(dashboard_name()))
    return render_template('login.html', role=role)


@app.route('/forgot-password/<role>', methods=['GET', 'POST'])
def forgot_password(role):
    if role not in {'student', 'teacher', 'admin'}:
        abort(404)
    demo_link = None
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        with get_db() as conn:
            user = conn.execute(
                '''SELECT * FROM users
                   WHERE role=? AND (lower(email)=lower(?) OR lower(username)=lower(?))''',
                (role, identifier, identifier)
            ).fetchone()
            if not user:
                flash('No account found with that username or email.', 'danger')
                return redirect(request.url)
            token = build_reset_token(user['id'])
            demo_link = url_for('reset_password', token=token)
            flash('Password reset link generated below for demo use.', 'success')
    return render_template('forgot_password.html', role=role, demo_link=demo_link)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    with get_db() as conn:
        record = conn.execute(
            'SELECT pr.*, u.username, u.role FROM password_resets pr JOIN users u ON u.id = pr.user_id WHERE token = ?',
            (token,)
        ).fetchone()
        if not record:
            flash('Invalid reset token.', 'danger')
            return redirect(url_for('index'))
        if parse_dt(record['expires_at']) < now_dt():
            flash('Reset token expired.', 'danger')
            return redirect(url_for('index'))
        if request.method == 'POST':
            password = request.form.get('password', '')
            if len(password) < 8:
                flash('Password must be at least 8 characters.', 'danger')
                return redirect(request.url)
            conn.execute('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(password), record['user_id']))
            conn.execute('DELETE FROM password_resets WHERE user_id=?', (record['user_id'],))
            conn.commit()
            flash('Password reset successful. Please log in.', 'success')
            return redirect(url_for('login', role=record['role']))
    return render_template('reset_password.html', token=token)


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('index'))


@app.route('/admin/dashboard')
@login_required('admin')
def admin_dashboard():
    branch = selected_branch('ALL')
    sem = selected_sem('ALL')
    with get_db() as conn:
        pending_teachers = conn.execute(
            '''SELECT * FROM users WHERE role='teacher' AND is_approved=0 AND is_rejected=0
               AND (?='ALL' OR branch=?) ORDER BY created_at DESC''',
            (branch, branch)
        ).fetchall()
        teachers = conn.execute(
            '''SELECT * FROM users WHERE role='teacher' AND (?='ALL' OR branch=?) ORDER BY created_at DESC''',
            (branch, branch)
        ).fetchall()
        students = conn.execute(
            '''SELECT * FROM users WHERE role='student'
               AND (?='ALL' OR branch=?) AND (?='ALL' OR sem=?) ORDER BY created_at DESC''',
            (branch, branch, sem, sem)
        ).fetchall()
        tests = conn.execute(
            '''SELECT t.*, u.full_name AS teacher_name,
                      (SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) AS question_count,
                      (SELECT COUNT(*) FROM attempts a WHERE a.test_id=t.id) AS attempt_count
               FROM tests t JOIN users u ON u.id=t.teacher_id
               ORDER BY datetime(replace(t.scheduled_at,'T',' ')) DESC'''
        ).fetchall()
        leaderboard = conn.execute(
            '''SELECT a.*, u.full_name, u.username, u.branch, u.sem, t.title,
                      RANK() OVER (PARTITION BY a.test_id, u.branch, u.sem ORDER BY a.score DESC, a.completed_at ASC) AS test_rank
               FROM attempts a JOIN users u ON u.id=a.student_id JOIN tests t ON t.id=a.test_id
               WHERE a.status IN ('completed','disqualified')
                 AND (?='ALL' OR u.branch=?) AND (?='ALL' OR u.sem=?)
               ORDER BY a.completed_at DESC LIMIT 20''',
            (branch, branch, sem, sem)
        ).fetchall()
        violations = conn.execute(
            '''SELECT v.*, u.username, u.branch, u.sem, t.title
               FROM violations v JOIN attempts a ON a.id=v.attempt_id
               JOIN users u ON u.id=a.student_id JOIN tests t ON t.id=a.test_id
               WHERE (?='ALL' OR u.branch=?) AND (?='ALL' OR u.sem=?)
               ORDER BY v.created_at DESC LIMIT 25''',
            (branch, branch, sem, sem)
        ).fetchall()
    tests = [t for t in tests if branch == 'ALL' or has_target(t['target_branches'], 'ALL', branch, '1')]
    return render_template('admin_dashboard.html', pending_teachers=pending_teachers, teachers=teachers, students=students, tests=tests, selected_branch=branch, selected_sem=sem, leaderboard=leaderboard, violations=violations)


@app.route('/admin/teacher/<int:user_id>/status', methods=['POST'])
@login_required('admin')
def update_teacher_status(user_id):
    action = request.form.get('action', '').strip().lower()
    with get_db() as conn:
        teacher = conn.execute("SELECT * FROM users WHERE id=? AND role='teacher'", (user_id,)).fetchone()
        if not teacher:
            abort(404)
        if action == 'approve':
            conn.execute("UPDATE users SET is_approved=1, is_rejected=0, approved_by=?, approved_at=? WHERE id=?", (session['user_id'], now_ts(), user_id))
            flash('Teacher approved.', 'success')
        elif action == 'delete':
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            flash('Teacher removed.', 'warning')
        else:
            flash('Invalid action.', 'danger')
        conn.commit()
    return redirect(url_for('admin_dashboard', branch=request.args.get('branch', 'ALL'), sem=request.args.get('sem', 'ALL')))


@app.route('/admin/teacher/<int:user_id>')
@login_required('admin')
def teacher_details(user_id):
    with get_db() as conn:
        teacher = conn.execute(
            '''SELECT u.*,
                      admin.full_name AS approved_by_name,
                      (SELECT COUNT(*) FROM tests t WHERE t.teacher_id=u.id) AS test_count,
                      (SELECT COUNT(*) FROM questions q JOIN tests t ON t.id=q.test_id WHERE t.teacher_id=u.id) AS question_count,
                      (SELECT COUNT(*) FROM attempts a JOIN tests t ON t.id=a.test_id WHERE t.teacher_id=u.id) AS attempt_count
               FROM users u
               LEFT JOIN users admin ON admin.id=u.approved_by
               WHERE u.id=? AND u.role='teacher' ''',
            (user_id,)
        ).fetchone()
        if not teacher:
            abort(404)
        recent_tests = conn.execute(
            '''SELECT id, title, scheduled_at, end_at, is_active, target_branches, target_sems
               FROM tests WHERE teacher_id=?
               ORDER BY datetime(replace(scheduled_at,'T',' ')) DESC LIMIT 8''',
            (user_id,)
        ).fetchall()
    return render_template('teacher_details.html', teacher=teacher, recent_tests=recent_tests)


@app.route('/admin/test/<int:test_id>/delete', methods=['POST'])
@login_required('admin')
def admin_delete_test(test_id):
    with get_db() as conn:
        conn.execute('DELETE FROM tests WHERE id=?', (test_id,))
        conn.commit()
    flash('Test deleted.', 'success')
    return redirect(url_for('admin_dashboard', branch=request.args.get('branch', 'ALL'), sem=request.args.get('sem', 'ALL')))


@app.route('/teacher/test/<int:test_id>/delete', methods=['POST'])
@login_required('teacher')
def teacher_delete_test(test_id):
    with get_db() as conn:
        test = conn.execute('SELECT id FROM tests WHERE id=? AND teacher_id=?', (test_id, session['user_id'])).fetchone()
        if not test:
            abort(404)
        conn.execute('DELETE FROM tests WHERE id=?', (test_id,))
        conn.commit()
    flash('Test deleted.', 'success')
    return redirect(url_for('teacher_dashboard', branch=request.args.get('branch', 'ALL'), sem=request.args.get('sem', 'ALL')))


@app.route('/teacher/dashboard')
@login_required('teacher')
def teacher_dashboard():
    branch = selected_branch('ALL')
    sem = selected_sem('ALL')
    with get_db() as conn:
        tests = conn.execute(
            '''SELECT t.*,
                      (SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) AS question_count,
                      (SELECT COUNT(*) FROM attempts a WHERE a.test_id=t.id) AS attempt_count
               FROM tests t WHERE teacher_id=?
               ORDER BY datetime(replace(t.scheduled_at,'T',' ')) DESC''',
            (session['user_id'],)
        ).fetchall()
        students = conn.execute(
            '''SELECT * FROM users WHERE role='student'
               AND (?='ALL' OR branch=?) AND (?='ALL' OR sem=?)
               ORDER BY created_at DESC''',
            (branch, branch, sem, sem)
        ).fetchall()
    tests = [t for t in tests if branch == 'ALL' or has_target(t['target_branches'], 'ALL', branch, '1')]
    return render_template(
        'teacher_dashboard.html',
        tests=tests,
        students=students,
        selected_branch=branch,
        selected_sem=sem,
        teacher_branch=session.get('branch', 'GENERAL')
    )


@app.route('/teacher/test/create', methods=['GET', 'POST'])
@login_required('teacher')
def create_test():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        scheduled_at = request.form.get('scheduled_at', '').strip()
        end_at = request.form.get('end_at', '').strip()
        duration_minutes = int(request.form.get('duration_minutes', '30') or 30)
        target_branches = split_multiselect('target_branches', BRANCHES)
        target_sems = split_multiselect('target_sems', SEMS)
        if not title or not scheduled_at or not end_at or duration_minutes <= 0:
            flash('Please fill all required test details.', 'danger')
            return redirect(request.url)
        start_dt = parse_dt(scheduled_at)
        end_dt = parse_dt(end_at)
        if not start_dt or not end_dt or end_dt <= start_dt:
            flash('End time must be after start time.', 'danger')
            return redirect(request.url)
        answer_release_at = end_dt.strftime('%Y-%m-%dT%H:%M')
        with get_db() as conn:
            conn.execute(
                '''INSERT INTO tests (teacher_id, title, description, scheduled_at, duration_minutes, total_marks, answer_release_at, is_active, target_branches, target_sems, end_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (session['user_id'], title, description, scheduled_at, duration_minutes, 0, answer_release_at, 1, list_to_csv(target_branches), list_to_csv(target_sems), end_at)
            )
            conn.commit()
        flash('Test created successfully. Now add questions.', 'success')
        return redirect(url_for('teacher_dashboard'))
    return render_template('create_test.html')


@app.route('/test/<int:test_id>', methods=['GET', 'POST'])
@login_required()
@admin_or_teacher_required
def manage_test(test_id):
    branch = selected_branch('ALL')
    sem = selected_sem('ALL')
    with get_db() as conn:
        test = get_test_for_manager(conn, test_id)
        if not test:
            abort(404)
        if request.method == 'POST':
            question_text = request.form.get('question_text', '').strip()
            option_a = request.form.get('option_a', '').strip()
            option_b = request.form.get('option_b', '').strip()
            option_c = request.form.get('option_c', '').strip()
            option_d = request.form.get('option_d', '').strip()
            correct_answer = request.form.get('correct_answer', '').strip().upper()
            marks = int(request.form.get('marks', '1') or 1)
            if not all([question_text, option_a, option_b, option_c, option_d]) or correct_answer not in {'A', 'B', 'C', 'D'}:
                flash('Please add a valid MCQ with one correct answer.', 'danger')
                return redirect(request.url)
            conn.execute(
                '''INSERT INTO questions (test_id, question_text, option_a, option_b, option_c, option_d, correct_answer, marks)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (test_id, question_text, option_a, option_b, option_c, option_d, correct_answer, marks)
            )
            total = conn.execute('SELECT COALESCE(SUM(marks),0) AS total FROM questions WHERE test_id=?', (test_id,)).fetchone()['total']
            conn.execute('UPDATE tests SET total_marks=? WHERE id=?', (total, test_id))
            conn.commit()
            flash('Question added.', 'success')
            return redirect(request.url)

        questions = conn.execute('SELECT * FROM questions WHERE test_id=? ORDER BY id', (test_id,)).fetchall()
        leaderboard = conn.execute(
            '''SELECT a.*, u.full_name, u.username, u.branch, u.sem,
                      RANK() OVER (PARTITION BY a.test_id, u.branch, u.sem ORDER BY a.score DESC, a.completed_at ASC) AS test_rank
               FROM attempts a JOIN users u ON u.id=a.student_id
               WHERE a.test_id=? AND a.status IN ('completed','disqualified')
                 AND (?='ALL' OR u.branch=?) AND (?='ALL' OR u.sem=?)
               ORDER BY u.branch, u.sem, a.score DESC, a.completed_at ASC''',
            (test_id, branch, branch, sem, sem)
        ).fetchall()
        violations = conn.execute(
            '''SELECT v.*, u.username, u.branch, u.sem FROM violations v
               JOIN attempts a ON a.id=v.attempt_id JOIN users u ON u.id=a.student_id
               WHERE a.test_id=? AND (?='ALL' OR u.branch=?) AND (?='ALL' OR u.sem=?)
               ORDER BY v.created_at DESC''',
            (test_id, branch, branch, sem, sem)
        ).fetchall()
    return render_template('manage_test.html', test=test, questions=questions, leaderboard=leaderboard, violations=violations, selected_branch=branch, selected_sem=sem)


@app.route('/test/<int:test_id>/update', methods=['POST'])
@login_required()
@admin_or_teacher_required
def update_test(test_id):
    with get_db() as conn:
        test = get_test_for_manager(conn, test_id)
        if not test:
            abort(404)
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        scheduled_at = request.form.get('scheduled_at', '').strip()
        end_at = request.form.get('end_at', '').strip()
        duration_minutes = int(request.form.get('duration_minutes', '30') or 30)
        is_active = 1 if request.form.get('is_active') == '1' else 0
        target_branches = split_multiselect('target_branches', BRANCHES)
        target_sems = split_multiselect('target_sems', SEMS)
        start_dt = parse_dt(scheduled_at)
        end_dt = parse_dt(end_at)
        if not title or not start_dt or not end_dt or end_dt <= start_dt or duration_minutes <= 0:
            flash('Please enter valid test details. End time must be after start time.', 'danger')
            return redirect(url_for('manage_test', test_id=test_id))
        conn.execute(
            '''UPDATE tests SET title=?, description=?, scheduled_at=?, end_at=?, duration_minutes=?, answer_release_at=?, is_active=?, target_branches=?, target_sems=?
               WHERE id=?''',
            (title, description, scheduled_at, end_at, duration_minutes, end_at, is_active, list_to_csv(target_branches), list_to_csv(target_sems), test_id)
        )
        conn.commit()
    flash('Test details updated.', 'success')
    return redirect(url_for('manage_test', test_id=test_id))


@app.route('/question/<int:question_id>/update', methods=['POST'])
@login_required()
@admin_or_teacher_required
def update_question(question_id):
    with get_db() as conn:
        row = conn.execute('''SELECT q.*, t.id AS test_id, t.teacher_id FROM questions q JOIN tests t ON t.id=q.test_id WHERE q.id=?''', (question_id,)).fetchone()
        if not row or (session.get('role') == 'teacher' and row['teacher_id'] != session['user_id']):
            abort(404)
        question_text = request.form.get('question_text', '').strip()
        option_a = request.form.get('option_a', '').strip()
        option_b = request.form.get('option_b', '').strip()
        option_c = request.form.get('option_c', '').strip()
        option_d = request.form.get('option_d', '').strip()
        correct_answer = request.form.get('correct_answer', '').strip().upper()
        marks = int(request.form.get('marks', '1') or 1)
        if not all([question_text, option_a, option_b, option_c, option_d]) or correct_answer not in {'A', 'B', 'C', 'D'}:
            flash('Invalid question update.', 'danger')
            return redirect(url_for('manage_test', test_id=row['test_id']))
        conn.execute('''UPDATE questions SET question_text=?, option_a=?, option_b=?, option_c=?, option_d=?, correct_answer=?, marks=? WHERE id=?''',
                     (question_text, option_a, option_b, option_c, option_d, correct_answer, marks, question_id))
        total = conn.execute('SELECT COALESCE(SUM(marks),0) AS total FROM questions WHERE test_id=?', (row['test_id'],)).fetchone()['total']
        conn.execute('UPDATE tests SET total_marks=? WHERE id=?', (total, row['test_id']))
        conn.commit()
    flash('Question updated.', 'success')
    return redirect(url_for('manage_test', test_id=row['test_id']))


@app.route('/question/<int:question_id>/delete', methods=['POST'])
@login_required()
@admin_or_teacher_required
def delete_question(question_id):
    with get_db() as conn:
        row = conn.execute('''SELECT q.id, q.test_id, t.teacher_id FROM questions q JOIN tests t ON t.id=q.test_id WHERE q.id=?''', (question_id,)).fetchone()
        if not row or (session.get('role') == 'teacher' and row['teacher_id'] != session['user_id']):
            abort(404)
        conn.execute('DELETE FROM questions WHERE id=?', (question_id,))
        total = conn.execute('SELECT COALESCE(SUM(marks),0) AS total FROM questions WHERE test_id=?', (row['test_id'],)).fetchone()['total']
        conn.execute('UPDATE tests SET total_marks=? WHERE id=?', (total, row['test_id']))
        conn.commit()
    flash('Question deleted.', 'success')
    return redirect(url_for('manage_test', test_id=row['test_id']))


@app.route('/test/<int:test_id>/export')
@login_required()
@admin_or_teacher_required
def export_results(test_id):
    with get_db() as conn:
        test = get_test_for_manager(conn, test_id)
        if not test:
            abort(404)
        rows = conn.execute(
            '''SELECT u.full_name, u.username, u.email, u.branch, u.sem, a.status, a.score, t.total_marks, a.warnings_count, a.started_at, a.completed_at
               FROM attempts a JOIN users u ON u.id=a.student_id JOIN tests t ON t.id=a.test_id
               WHERE a.test_id=? ORDER BY u.branch, u.sem, a.score DESC, a.completed_at ASC''',
            (test_id,)
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Full Name', 'Username', 'Email', 'Branch', 'Sem', 'Status', 'Score', 'Total Marks', 'Warnings', 'Started At', 'Completed At'])
    for row in rows:
        writer.writerow([row['full_name'], row['username'], row['email'], row['branch'], row['sem'], row['status'], row['score'], row['total_marks'], row['warnings_count'], row['started_at'], row['completed_at']])
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename=test_{test_id}_results.csv'})


@app.route('/student/dashboard')
@login_required('student')
def student_dashboard():
    student = current_user()
    now_value = now_ts()
    with get_db() as conn:
        all_tests = conn.execute(
            '''SELECT t.*, u.full_name AS teacher_name FROM tests t JOIN users u ON u.id=t.teacher_id
               WHERE t.is_active=1 ORDER BY datetime(replace(t.scheduled_at,'T',' ')) ASC'''
        ).fetchall()
        upcoming_tests, available_tests = [], []
        for t in all_tests:
            if not has_target(t['target_branches'], t['target_sems'], student['branch'], student['sem']):
                continue
            start_dt = parse_dt(t['scheduled_at'])
            end_dt = parse_dt(t['end_at'])
            if not start_dt or not end_dt:
                continue
            if start_dt > now_dt():
                upcoming_tests.append(t)
            elif start_dt <= now_dt() <= end_dt:
                available_tests.append(t)
        attempts = conn.execute(
            '''SELECT a.*, t.title, t.answer_release_at, t.total_marks, t.target_branches, t.target_sems,
                      RANK() OVER (PARTITION BY a.test_id, u.branch, u.sem ORDER BY a.score DESC, a.completed_at ASC) AS test_rank
               FROM attempts a JOIN tests t ON t.id=a.test_id JOIN users u ON u.id=a.student_id
               WHERE a.student_id=? ORDER BY a.created_at DESC''',
            (session['user_id'],)
        ).fetchall()
    return render_template('student_dashboard.html', upcoming_tests=upcoming_tests, available_tests=available_tests, attempts=attempts, student=student)


@app.route('/student/test/<int:test_id>/start', methods=['POST'])
@login_required('student')
def start_test(test_id):
    with get_db() as conn:
        test = conn.execute('SELECT * FROM tests WHERE id=? AND is_active=1', (test_id,)).fetchone()
        student = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not test:
            flash('Test not found or inactive.', 'danger')
            return redirect(url_for('student_dashboard'))
        if not has_target(test['target_branches'], test['target_sems'], student['branch'], student['sem']):
            flash('This test is not assigned to your branch/sem.', 'danger')
            return redirect(url_for('student_dashboard'))
        start_dt = parse_dt(test['scheduled_at'])
        end_dt = parse_dt(test['end_at'])
        if not start_dt or not end_dt or now_dt() < start_dt:
            flash('This test has not started yet.', 'warning')
            return redirect(url_for('student_dashboard'))
        if now_dt() > end_dt:
            flash('This test has already ended.', 'warning')
            return redirect(url_for('student_dashboard'))
        finished = conn.execute("SELECT id FROM attempts WHERE test_id=? AND student_id=? AND status IN ('completed','disqualified')", (test_id, session['user_id'])).fetchone()
        started = conn.execute("SELECT id FROM attempts WHERE test_id=? AND student_id=? AND status='in_progress' ORDER BY id DESC LIMIT 1", (test_id, session['user_id'])).fetchone()
        if finished:
            flash('You have already finished this test.', 'warning')
            return redirect(url_for('student_dashboard'))
        if not started:
            conn.execute('''INSERT INTO attempts (test_id, student_id, started_at, status, warnings_count) VALUES (?, ?, ?, 'in_progress', 0)''', (test_id, session['user_id'], now_ts()))
            conn.commit()
    return redirect(url_for('take_test', test_id=test_id))


@app.route('/student/test/<int:test_id>/take')
@login_required('student')
def take_test(test_id):
    with get_db() as conn:
        test = conn.execute('SELECT * FROM tests WHERE id=? AND is_active=1', (test_id,)).fetchone()
        questions = conn.execute('SELECT * FROM questions WHERE test_id=? ORDER BY id', (test_id,)).fetchall()
        attempt = conn.execute("SELECT * FROM attempts WHERE test_id=? AND student_id=? AND status='in_progress' ORDER BY id DESC LIMIT 1", (test_id, session['user_id'])).fetchone()
        student = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not test or not attempt:
            flash('No active attempt found for this test.', 'danger')
            return redirect(url_for('student_dashboard'))
        if not has_target(test['target_branches'], test['target_sems'], student['branch'], student['sem']):
            flash('This test is not assigned to your branch/sem.', 'danger')
            return redirect(url_for('student_dashboard'))
        end_dt = parse_dt(test['end_at'])
        if end_dt and now_dt() > end_dt:
            conn.execute("UPDATE attempts SET completed_at=COALESCE(completed_at, ?), status='completed' WHERE id=?", (now_ts(), attempt['id']))
            conn.commit()
            flash('This test has already ended.', 'warning')
            return redirect(url_for('student_dashboard'))
        if not questions:
            flash('This test has no questions yet. Contact your teacher.', 'warning')
            return redirect(url_for('student_dashboard'))
        saved_answers = conn.execute('SELECT question_id, selected_answer FROM answers WHERE attempt_id=?', (attempt['id'],)).fetchall()
        answer_map = {row['question_id']: row['selected_answer'] for row in saved_answers}
    return render_template('take_test.html', test=test, questions=questions, attempt=attempt, answer_map=answer_map, max_violations=app.config['MAX_VIOLATIONS'])


@app.route('/student/test/<int:test_id>/autosave', methods=['POST'])
@login_required('student')
def autosave_answer(test_id):
    data = request.get_json(force=True)
    question_id = data.get('question_id')
    selected_answer = (data.get('selected_answer') or '').upper()
    if selected_answer not in {'A', 'B', 'C', 'D'}:
        return jsonify({'ok': False, 'message': 'Invalid answer'}), 400
    with get_db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE test_id=? AND student_id=? AND status='in_progress' ORDER BY id DESC LIMIT 1", (test_id, session['user_id'])).fetchone()
        if not attempt:
            return jsonify({'ok': False, 'message': 'No active attempt'}), 404
        existing = conn.execute('SELECT id FROM answers WHERE attempt_id=? AND question_id=?', (attempt['id'], question_id)).fetchone()
        if existing:
            conn.execute('UPDATE answers SET selected_answer=?, updated_at=? WHERE id=?', (selected_answer, now_ts(), existing['id']))
        else:
            conn.execute('INSERT INTO answers (attempt_id, question_id, selected_answer, updated_at) VALUES (?, ?, ?, ?)', (attempt['id'], question_id, selected_answer, now_ts()))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/student/test/<int:test_id>/violation', methods=['POST'])
@login_required('student')
def log_violation(test_id):
    data = request.get_json(force=True)
    violation_type = data.get('violation_type', 'unknown')
    details = data.get('details', '')
    if violation_type == 'window_blur':
        return jsonify({'ok': True, 'warnings': 0, 'terminated': False, 'ignored': True})
    with get_db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE test_id=? AND student_id=? AND status='in_progress' ORDER BY id DESC LIMIT 1", (test_id, session['user_id'])).fetchone()
        if not attempt:
            return jsonify({'ok': False, 'message': 'No active attempt'}), 404
        warnings = attempt['warnings_count'] + 1
        conn.execute('INSERT INTO violations (attempt_id, violation_type, details, created_at) VALUES (?, ?, ?, ?)', (attempt['id'], violation_type, details, now_ts()))
        status = 'disqualified' if warnings >= app.config['MAX_VIOLATIONS'] else 'in_progress'
        conn.execute('UPDATE attempts SET warnings_count=?, status=?, completed_at=CASE WHEN ?="disqualified" THEN ? ELSE completed_at END WHERE id=?', (warnings, status, status, now_ts(), attempt['id']))
        conn.commit()
    return jsonify({'ok': True, 'warnings': warnings, 'terminated': warnings >= app.config['MAX_VIOLATIONS']})


@app.route('/student/test/<int:test_id>/submit', methods=['POST'])
@login_required('student')
def submit_test(test_id):
    with get_db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE test_id=? AND student_id=? AND status='in_progress' ORDER BY id DESC LIMIT 1", (test_id, session['user_id'])).fetchone()
        if not attempt:
            flash('No active attempt to submit.', 'warning')
            return redirect(url_for('student_dashboard'))
        questions = conn.execute('SELECT * FROM questions WHERE test_id=?', (test_id,)).fetchall()
        answers = conn.execute('SELECT * FROM answers WHERE attempt_id=?', (attempt['id'],)).fetchall()
        answer_map = {a['question_id']: a['selected_answer'] for a in answers}
        score = sum(q['marks'] for q in questions if answer_map.get(q['id']) == q['correct_answer'])
        total_marks = sum(q['marks'] for q in questions)
        conn.execute("UPDATE attempts SET score=?, completed_at=?, status='completed' WHERE id=?", (score, now_ts(), attempt['id']))
        conn.commit()
    flash(f'Test submitted. Score: {score}/{total_marks}', 'success')
    return redirect(url_for('student_dashboard'))


@app.route('/student/result/<int:attempt_id>')
@login_required('student')
def view_result(attempt_id):
    with get_db() as conn:
        attempt = conn.execute(
            '''SELECT a.*, t.title, t.answer_release_at, t.total_marks, t.end_at,
                      u.branch AS student_branch, u.sem AS student_sem
               FROM attempts a
               JOIN tests t ON t.id=a.test_id
               JOIN users u ON u.id=a.student_id
               WHERE a.id=? AND a.student_id=?''',
            (attempt_id, session['user_id'])
        ).fetchone()
        if not attempt:
            abort(404)
        questions = conn.execute('SELECT * FROM questions WHERE test_id=?', (attempt['test_id'],)).fetchall()
        answers = conn.execute('SELECT * FROM answers WHERE attempt_id=?', (attempt_id,)).fetchall()
        answer_map = {a['question_id']: a['selected_answer'] for a in answers}
        release_dt = parse_dt(attempt['answer_release_at']) or parse_dt(attempt['end_at'])
        can_see_answers = release_dt and release_dt <= now_dt()
        scope_branch = attempt['student_branch'] or session.get('branch', 'GENERAL')
        scope_sem = attempt['student_sem'] or session.get('sem', '1')

        leaderboard = conn.execute(
            '''SELECT a.id, a.score, a.status, a.warnings_count, u.username, u.full_name,
                      RANK() OVER (ORDER BY a.score DESC, a.completed_at ASC) AS test_rank
               FROM attempts a
               JOIN users u ON u.id = a.student_id
               WHERE a.test_id=? AND u.branch=? AND u.sem=? AND a.status!='in_progress'
               ORDER BY a.score DESC, a.completed_at ASC''',
            (attempt['test_id'], scope_branch, scope_sem)
        ).fetchall()

        violations = conn.execute(
            '''SELECT violation_type, details, created_at
               FROM violations
               WHERE attempt_id=?
               ORDER BY created_at DESC''',
            (attempt_id,)
        ).fetchall()

    attempt_data = dict(attempt)
    attempt_data['branch'] = scope_branch
    attempt_data['sem'] = scope_sem
    return render_template(
        'result.html',
        attempt=attempt_data,
        questions=questions,
        answer_map=answer_map,
        can_see_answers=can_see_answers,
        leaderboard=leaderboard,
        violations=violations
    )


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'time': now_ts()})


@app.errorhandler(403)
def forbidden(_):
    return render_template('error.html', code=403, message='Access denied.'), 403


@app.errorhandler(404)
def not_found(_):
    return render_template('error.html', code=404, message='Page not found.'), 404


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
