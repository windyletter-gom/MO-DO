
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "moa_work.db"

app = FastAPI(title="MOA WORK v3.0")
app.mount("/static", StaticFiles(directory=BASE/"static"), name="static")

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

@app.get("/")
def root():
    return FileResponse(BASE/"static"/"index.html")

def status_order(s):
    return {"TODO":1,"IN_PROGRESS":2,"BLOCKED":3,"REVIEW":4,"DONE":5}.get(s,9)

def log(c,task_id,actor_id,action,old_value,new_value):
    c.execute("INSERT INTO activity_logs(task_id,actor_id,action,old_value,new_value) VALUES(?,?,?,?,?)",
              (task_id,actor_id,action,old_value,new_value))

@app.get("/api/users")
def users():
    with conn() as c:
        return [dict(r) for r in c.execute("""
            SELECT u.*, t.name team_name
            FROM users u LEFT JOIN teams t ON u.team_id=t.id
            ORDER BY CASE WHEN u.role='DIVISION_ADMIN' THEN 0 ELSE 1 END, t.sort_order, u.is_team_leader DESC, u.id
        """)]

@app.get("/api/teams")
def teams():
    with conn() as c:
        rows = c.execute("SELECT * FROM teams ORDER BY sort_order").fetchall()
        out=[]
        for r in rows:
            d=dict(r)
            d["members"]=[dict(x) for x in c.execute("SELECT * FROM users WHERE team_id=? ORDER BY is_team_leader DESC,id",(r["id"],))]
            out.append(d)
        return out

@app.get("/api/projects")
def projects():
    with conn() as c:
        return [dict(r) for r in c.execute("""
            SELECT p.*, u.name pm_name,
            (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id) task_count,
            (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status='DONE') done_count
            FROM projects p LEFT JOIN users u ON p.pm_user_id=u.id
            ORDER BY p.id DESC
        """)]

class ProjectIn(BaseModel):
    name:str
    pm_user_id:Optional[int]=None
    due_date:Optional[str]=None
    description:Optional[str]=""

@app.post("/api/projects")
def create_project(x:ProjectIn):
    with conn() as c:
        cur=c.execute("INSERT INTO projects(name,pm_user_id,due_date,description,status) VALUES(?,?,?,?,?)",
                      (x.name,x.pm_user_id,x.due_date,x.description,"ACTIVE"))
        c.commit()
        return {"id":cur.lastrowid}

@app.get("/api/tasks")
def tasks(project_id:Optional[int]=None, assignee_id:Optional[int]=None, team_id:Optional[int]=None):
    q="""
    SELECT t.*, p.name project_name, u.name assignee_name, tm.name team_name
    FROM tasks t
    JOIN projects p ON t.project_id=p.id
    LEFT JOIN users u ON t.assignee_id=u.id
    LEFT JOIN teams tm ON t.team_id=tm.id
    WHERE 1=1
    """
    args=[]
    if project_id:
        q+=" AND t.project_id=?"; args.append(project_id)
    if assignee_id:
        q+=" AND t.assignee_id=?"; args.append(assignee_id)
    if team_id:
        q+=" AND t.team_id=?"; args.append(team_id)
    q+=" ORDER BY t.status_order, t.priority_order, t.id"
    with conn() as c:
        return [dict(r) for r in c.execute(q,args)]

class TaskIn(BaseModel):
    project_id:int
    title:str
    team_id:Optional[int]=None
    assignee_id:Optional[int]=None
    priority:str="NORMAL"
    status:str="TODO"
    due_date:Optional[str]=None
    description:Optional[str]=""

@app.post("/api/tasks")
def create_task(x:TaskIn):
    with conn() as c:
        maxo=c.execute("SELECT COALESCE(MAX(priority_order),0)+1 FROM tasks WHERE project_id=? AND status=?",(x.project_id,x.status)).fetchone()[0]
        cur=c.execute("""INSERT INTO tasks(project_id,title,team_id,assignee_id,priority,status,due_date,description,priority_order,status_order)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",(x.project_id,x.title,x.team_id,x.assignee_id,x.priority,x.status,x.due_date,x.description,maxo,status_order(x.status)))
        log(c,cur.lastrowid,None,"CREATE",None,x.title)
        c.commit()
        return {"id":cur.lastrowid}

class MoveIn(BaseModel):
    status:str
    order:int
    actor_id:Optional[int]=None

@app.patch("/api/tasks/{task_id}/move")
def move_task(task_id:int,x:MoveIn):
    with conn() as c:
        r=c.execute("SELECT status FROM tasks WHERE id=?",(task_id,)).fetchone()
        if not r: raise HTTPException(404)
        c.execute("UPDATE tasks SET status=?, status_order=?, priority_order=? WHERE id=?",(x.status,status_order(x.status),x.order,task_id))
        log(c,task_id,x.actor_id,"MOVE",r["status"],x.status)
        c.commit()
        return {"ok":True}

class PriorityIn(BaseModel):
    priority:str
    order:Optional[int]=None
    actor_id:Optional[int]=None
    reason:Optional[str]=""

@app.patch("/api/tasks/{task_id}/priority")
def priority(task_id:int,x:PriorityIn):
    with conn() as c:
        r=c.execute("SELECT priority,priority_order FROM tasks WHERE id=?",(task_id,)).fetchone()
        if not r: raise HTTPException(404)
        order=x.order if x.order is not None else r["priority_order"]
        c.execute("UPDATE tasks SET priority=?, priority_order=? WHERE id=?",(x.priority,order,task_id))
        log(c,task_id,x.actor_id,"PRIORITY",r["priority"],x.priority + (f" / {x.reason}" if x.reason else ""))
        c.commit()
        return {"ok":True}

@app.get("/api/dashboard")
def dashboard(user_id:Optional[int]=None):
    with conn() as c:
        d={}
        d["project_count"]=c.execute("SELECT COUNT(*) FROM projects WHERE status='ACTIVE'").fetchone()[0]
        d["task_count"]=c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        d["delayed_count"]=c.execute("SELECT COUNT(*) FROM tasks WHERE due_date<date('now') AND status!='DONE'").fetchone()[0]
        d["urgent_count"]=c.execute("SELECT COUNT(*) FROM tasks WHERE priority='URGENT' AND status!='DONE'").fetchone()[0]
        d["blocked_count"]=c.execute("SELECT COUNT(*) FROM tasks WHERE status='BLOCKED'").fetchone()[0]
        if user_id:
            d["my_tasks"]=c.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status!='DONE'",(user_id,)).fetchone()[0]
            d["incoming_requests"]=c.execute("SELECT COUNT(*) FROM requests WHERE target_user_id=? AND status!='DONE'",(user_id,)).fetchone()[0]
            d["outgoing_requests"]=c.execute("SELECT COUNT(*) FROM requests WHERE requester_id=? AND status!='DONE'",(user_id,)).fetchone()[0]
        return d

@app.get("/api/requests")
def requests(user_id:Optional[int]=None, direction:str="incoming"):
    with conn() as c:
        q="""SELECT r.*, u1.name requester_name,u2.name target_name,t.name target_team_name
             FROM requests r
             LEFT JOIN users u1 ON r.requester_id=u1.id
             LEFT JOIN users u2 ON r.target_user_id=u2.id
             LEFT JOIN teams t ON r.target_team_id=t.id WHERE 1=1"""
        args=[]
        if user_id:
            if direction=="incoming":
                q+=" AND (r.target_user_id=? OR (r.target_user_id IS NULL AND r.target_team_id=(SELECT team_id FROM users WHERE id=?)))"
                args += [user_id,user_id]
            else:
                q+=" AND r.requester_id=?"; args.append(user_id)
        q+=" ORDER BY r.id DESC"
        return [dict(x) for x in c.execute(q,args)]

class RequestIn(BaseModel):
    requester_id:int
    target_user_id:Optional[int]=None
    target_team_id:Optional[int]=None
    type:str="TASK"
    title:str
    content:Optional[str]=""
    priority:str="NORMAL"
    due_date:Optional[str]=None

@app.post("/api/requests")
def create_request(x:RequestIn):
    with conn() as c:
        cur=c.execute("""INSERT INTO requests(requester_id,target_user_id,target_team_id,type,title,content,status,priority,due_date)
        VALUES(?,?,?,?,?,?,?,?,?)""",(x.requester_id,x.target_user_id,x.target_team_id,x.type,x.title,x.content,"WAITING",x.priority,x.due_date))
        c.commit()
        return {"id":cur.lastrowid}

# ---------- Shared Calendar ----------
@app.get("/api/calendar")
def calendar(month:Optional[str]=None, team_id:Optional[int]=None, event_type:Optional[str]=None):
    q="""SELECT ce.*, t.name team_name, u.name owner_name
         FROM calendar_events ce
         LEFT JOIN teams t ON ce.team_id=t.id
         LEFT JOIN users u ON ce.owner_id=u.id
         WHERE 1=1"""
    args=[]
    if month:
        q+=" AND substr(ce.start_date,1,7)=?"; args.append(month)
    if team_id:
        q+=" AND ce.team_id=?"; args.append(team_id)
    if event_type:
        q+=" AND ce.event_type=?"; args.append(event_type)
    q+=" ORDER BY ce.start_date,ce.id"
    with conn() as c:
        rows=[dict(r) for r in c.execute(q,args)]
        # task deadlines are merged into the shared calendar
        tq="""SELECT tsk.id,tsk.title,tsk.due_date,tsk.team_id,tm.name team_name,u.name owner_name
              FROM tasks tsk LEFT JOIN teams tm ON tsk.team_id=tm.id LEFT JOIN users u ON tsk.assignee_id=u.id
              WHERE tsk.due_date IS NOT NULL"""
        targs=[]
        if month:
            tq+=" AND substr(tsk.due_date,1,7)=?";targs.append(month)
        if team_id:
            tq+=" AND tsk.team_id=?";targs.append(team_id)
        for r in c.execute(tq,targs):
            d=dict(r)
            rows.append({
                "id":f"task-{d['id']}","title":d["title"],"start_date":d["due_date"],"end_date":d["due_date"],
                "event_type":"TASK_DUE","team_id":d["team_id"],"team_name":d["team_name"],"owner_name":d["owner_name"],
                "description":"업무 마감일","source":"TASK"
            })
        rows.sort(key=lambda x:(x["start_date"],str(x["id"])))
        return rows

class CalendarIn(BaseModel):
    title:str
    start_date:str
    end_date:Optional[str]=None
    event_type:str="GENERAL"
    team_id:Optional[int]=None
    owner_id:Optional[int]=None
    description:Optional[str]=""

@app.post("/api/calendar")
def create_calendar(x:CalendarIn):
    with conn() as c:
        cur=c.execute("""INSERT INTO calendar_events(title,start_date,end_date,event_type,team_id,owner_id,description)
        VALUES(?,?,?,?,?,?,?)""",(x.title,x.start_date,x.end_date or x.start_date,x.event_type,x.team_id,x.owner_id,x.description))
        c.commit()
        return {"id":cur.lastrowid}

# ---------- Team workspace common modules ----------
@app.get("/api/workspace/{team_id}")
def workspace(team_id:int):
    with conn() as c:
        team=c.execute("SELECT * FROM teams WHERE id=?",(team_id,)).fetchone()
        if not team: raise HTTPException(404)
        modules=[dict(r) for r in c.execute("""
            SELECT m.*, COALESCE(tm.enabled,0) enabled
            FROM modules m LEFT JOIN team_modules tm ON m.id=tm.module_id AND tm.team_id=?
            ORDER BY m.sort_order
        """,(team_id,))]
        boards=[dict(r) for r in c.execute("SELECT * FROM data_boards WHERE team_id=? ORDER BY id",(team_id,))]
        meetings=[dict(r) for r in c.execute("SELECT * FROM meetings WHERE team_id=? ORDER BY meeting_date DESC LIMIT 10",(team_id,))]
        checklists=[dict(r) for r in c.execute("SELECT * FROM checklists WHERE team_id=? ORDER BY id DESC",(team_id,))]
        resources=[dict(r) for r in c.execute("SELECT * FROM resources WHERE team_id=? ORDER BY id DESC",(team_id,))]
        return {"team":dict(team),"modules":modules,"boards":boards,"meetings":meetings,"checklists":checklists,"resources":resources}

@app.get("/api/boards/{board_id}")
def board(board_id:int):
    with conn() as c:
        b=c.execute("SELECT * FROM data_boards WHERE id=?",(board_id,)).fetchone()
        if not b: raise HTTPException(404)
        cols=[dict(r) for r in c.execute("SELECT * FROM data_board_columns WHERE board_id=? ORDER BY sort_order,id",(board_id,))]
        rows=[]
        for r in c.execute("SELECT * FROM data_board_rows WHERE board_id=? ORDER BY id DESC",(board_id,)):
            d=dict(r)
            d["values"]={x["column_key"]:x["value"] for x in c.execute("SELECT * FROM data_board_values WHERE row_id=?",(r["id"],))}
            rows.append(d)
        return {"board":dict(b),"columns":cols,"rows":rows}

class MeetingIn(BaseModel):
    team_id:int
    title:str
    meeting_date:str
    attendees:Optional[str]=""
    decisions:Optional[str]=""
    action_items:Optional[str]=""

@app.post("/api/meetings")
def create_meeting(x:MeetingIn):
    with conn() as c:
        cur=c.execute("""INSERT INTO meetings(team_id,title,meeting_date,attendees,decisions,action_items)
        VALUES(?,?,?,?,?,?)""",(x.team_id,x.title,x.meeting_date,x.attendees,x.decisions,x.action_items))
        c.commit()
        return {"id":cur.lastrowid}

class ChecklistIn(BaseModel):
    team_id:int
    title:str
    recurrence:Optional[str]="NONE"
    items:Optional[str]=""

@app.post("/api/checklists")
def create_checklist(x:ChecklistIn):
    with conn() as c:
        cur=c.execute("INSERT INTO checklists(team_id,title,recurrence,items) VALUES(?,?,?,?)",
                      (x.team_id,x.title,x.recurrence,x.items))
        c.commit()
        return {"id":cur.lastrowid}

class ResourceIn(BaseModel):
    team_id:int
    title:str
    resource_type:Optional[str]="LINK"
    url:Optional[str]=""
    description:Optional[str]=""

@app.post("/api/resources")
def create_resource(x:ResourceIn):
    with conn() as c:
        cur=c.execute("INSERT INTO resources(team_id,title,resource_type,url,description) VALUES(?,?,?,?,?)",
                      (x.team_id,x.title,x.resource_type,x.url,x.description))
        c.commit()
        return {"id":cur.lastrowid}

@app.get("/api/activity")
def activity(task_id:Optional[int]=None):
    with conn() as c:
        q="""SELECT a.*,u.name actor_name FROM activity_logs a LEFT JOIN users u ON a.actor_id=u.id WHERE 1=1"""
        args=[]
        if task_id:
            q+=" AND task_id=?";args.append(task_id)
        q+=" ORDER BY a.id DESC LIMIT 100"
        return [dict(r) for r in c.execute(q,args)]
