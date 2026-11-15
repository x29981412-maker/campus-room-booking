"use strict";

const $ = id => document.getElementById(id);
const ZONE = new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit"});
const today = ZONE.format(new Date());
const dayAfter = (day, n) => new Date(Date.parse(day + "T00:00:00Z") + n * 86400000).toISOString().slice(0,10);
const hm = n => String(Math.floor(n/60)).padStart(2,"0") + ":" + String(n%60).padStart(2,"0");
const timeValue = time => Number(time.slice(0,2))*60 + Number(time.slice(3));
const stateNames = {confirmed:"已确认",pending:"待审批",checked_in:"已签到",completed:"已结束",
                    no_show:"爽约",expired:"审批超时",rejected:"已拒绝",cancelled:"已取消"};
const actionsChinese = {created:"提交预约",confirmed:"审批通过",rejected:"审批拒绝",cancelled:"取消预约",
    checked_in:"签到",completed:"结束",no_show:"爽约释放",expired:"审批超时",room_saved:"保存房间",
    closure_created:"新增封闭",closure_removed:"解除封闭"};
const oldKey = "study-room-bookings";
let studentDay = null;
let mode = "day";
let generation = 0;
let session = {role:null};
let adminData = null;

function make(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}
function button(text, handler, cls="secondary") {
  const el = make("button", cls, text);
  el.type = "button";
  el.addEventListener("click", () => run(handler));
  return el;
}
function notice(text, error=false) {
  const el=$("notice");
  el.hidden = !text;
  el.className = error ? "notice error" : "notice";
  el.textContent = text;
}
async function run(handler) {
  try { await handler(); } catch (error) { notice(error.message,true); }
}
async function api(path, method="GET", data) {
  const init = {method};
  if (data !== undefined) {
    init.headers = {"Content-Type":"application/json"};
    init.body = JSON.stringify(data);
  }
  const response = await fetch(path,init);
  const json = await response.json();
  if (!response.ok) throw new Error(typeof json.detail === "string" ? json.detail : "请检查所填信息。");
  return json;
}
const overlap = (list,a,b) => list.some(([x,y]) => x < b && y > a);
const blocked = room => [...room.reserved,...room.closed];
const chosen = () => studentDay?.rooms.find(room => room.id === $("room").value);
const shownRooms = () => studentDay.rooms.filter(room => room.capacity >= Number($("room-filter").value));
function fillOptions(control, values, selected) {
  control.replaceChildren(...values.map(([value,label])=>new Option(label,String(value))));
  if (values.some(([v])=>String(v)===String(selected))) control.value = String(selected);
}
function renderEnd() {
  const room = chosen();
  const begin = Number($("start").value);
  const list = [];
  if (room && $("start").value) {
    for (let t=begin+30;t<=Math.min(begin+240,studentDay.close);t+=30) {
      if (overlap(blocked(room),begin,t)) break;
      list.push([t,hm(t)]);
    }
  }
  fillOptions($("end"),list,$("end").value);
  $("submit").disabled = !list.length || session.role === "admin";
}
function renderStart(startAt=$("start").value) {
  const room = chosen();
  const list = [];
  if (room) {
    for(let t=studentDay.open;t<studentDay.close;t+=30) {
      if (t>=studentDay.not_before && !overlap(blocked(room),t,t+30)) list.push([t,hm(t)]);
    }
    $("people").max = room.capacity;
  }
  $("rule-note").textContent = room?.approval ? "这个房间需要管理员审批。" : "";
  fillOptions($("start"),list,startAt);
  renderEnd();
}
function renderRooms() {
  const list = shownRooms();
  fillOptions($("room"),list.map(r=>[r.id,r.name]),$("room").value);
  $("room-list").replaceChildren();
  if (!list.length) $("room-list").append(make("p","muted","没有符合条件的研讨室。"));
  for (const room of list) {
    const card = make("article","room-card");
    const heading = make("div");
    heading.append(make("h3",null,room.name),make("p","tags",
      [room.area,room.capacity+" 人",room.equipment,room.approval?"需审核":"即时确认"].filter(Boolean).join(" · ")));
    const grid = make("div","slots");
    for(let t=studentDay.open;t<studentDay.close;t+=30) {
      const isClosed = overlap(room.closed,t,t+30);
      const busy = overlap(room.reserved,t,t+30);
      const past = t < studentDay.not_before;
      const slot = button(hm(t),() => {
        $("room").value = room.id;
        renderStart(t);
        $("title").focus();
      },"slot");
      const label = isClosed?"封闭":busy?"已预约":past?"已过":"空闲";
      slot.disabled = isClosed || busy || past;
      if (isClosed || busy) slot.classList.add(isClosed?"closed":"busy");
      slot.setAttribute("aria-label",room.name+" "+hm(t)+" "+label);
      slot.title = label;
      grid.append(slot);
    }
    card.append(heading,grid);
    $("room-list").append(card);
  }
  renderStart();
}
function renderCalendar(calendar) {
  const section = $("calendar");
  section.hidden = !calendar;
  section.replaceChildren();
  if (!calendar) return;
  const grid = make("div","calendar-grid");
  if (mode === "month") {
    const first = new Date(calendar.days[0].date+"T00:00:00Z").getUTCDay();
    for (let i=0;i<(first+6)%7;i++) grid.append(make("span"));
  }
  for(const date of calendar.days) {
    const el = button(date.date.slice(5),async () => {
      $("day").value = date.date;
      await refreshStudent();
    },date.date === $("day").value ? "selected" : "");
    el.disabled = date.date < $("day").min || date.date > $("day").max;
    el.append(make("small",null,date.booked+" 条预约"));
    grid.append(el);
  }
  section.append(grid);
}
async function refreshStudent() {
  const request = ++generation;
  const day = $("day").value;
  $("submit").disabled = true;
  const [space, cal] = await Promise.all([
    api("/api/rooms?day="+encodeURIComponent(day)),
    mode === "day" ? null : api("/api/calendar?day="+encodeURIComponent(day)+"&view="+mode),
  ]);
  if (request !== generation) return;
  studentDay = space;
  renderCalendar(cal);
  renderRooms();
  await loadMine();
}
async function ensureStudent() {
  if (!session.role) session = {...session,...await api("/api/session/student","POST")};
}
function legacyBookings() {
  try {
    const saved = JSON.parse(localStorage.getItem(oldKey) || "[]");
    return Array.isArray(saved) ? saved.filter(x => x && typeof x.token === "string") : [];
  } catch { return []; }
}
function legacyRemove(token) {
  localStorage.setItem(oldKey,JSON.stringify(legacyBookings().filter(row=>row.token!==token)));
}
function localNow() {
  const now = new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).format(new Date());
  return timeValue(now);
}
function bookingRow(item, legacy=false) {
  const row = make("div","row");
  const info = make("div");
  const state = make("span","state",stateNames[item.status] || "旧版预约");
  state.dataset.state = item.status;
  info.append(state,make("strong",null,item.title),make("p",null,
    [item.day,item.room_name || item.room,hm(item.start)+"–"+hm(item.end),
    item.attendees+" 人",item.series?"连续预约":""].filter(Boolean).join(" · ")));
  if(item.decision) info.append(make("p",null,item.decision));
  const controls = make("div","actions");
  if(legacy || ["pending","confirmed"].includes(item.status)) {
    const current = localNow();
    if(!legacy && item.status==="confirmed" && item.day===ZONE.format(new Date())
        && current>=item.start-10 && current<Math.min(item.end,item.start+15)) {
      controls.append(button("签到",async()=>{
        await api("/api/bookings/check-in","POST",{token:item.token});
        notice("签到成功。");
        await refreshStudent();
      },"primary"));
    }
    controls.append(button("取消预约",async()=>{
      await api("/api/bookings/cancel","POST",{token:item.token});
      if (legacy) legacyRemove(item.token);
      notice("预约已取消。");
      await refreshStudent();
    }));
  }
  row.append(info,controls);
  return row;
}
async function loadMine() {
  const mine = $("mine");
  const bookings = session.role === "student" ? await api("/api/bookings/mine") : [];
  const fallback = legacyBookings().filter(item=>!bookings.some(b=>b.token===item.token));
  mine.replaceChildren(...bookings.map(item=>bookingRow(item)),...fallback.map(item=>bookingRow(item,true)));
  if (!mine.children.length) mine.append(make("p","muted",session.role==="admin"?"退出管理后查看学生预约。":"暂无预约。"));
}
function range() {
  return new URLSearchParams({first:$("report-first").value,last:$("report-last").value}).toString();
}
function editRoom(room) {
  $("edit-id").value=room.id; $("edit-name").value=room.name; $("edit-area").value=room.area;
  $("edit-cap").value=room.capacity; $("edit-equip").value=room.equipment;
  $("edit-active").checked=!!room.active; $("edit-approval").checked=!!room.approval;
  $("edit-name").focus();
}
function adminQueue() {
  const queue=$("queue");
  const status=$("queue-filter").value;
  queue.replaceChildren();
  const entries=adminData.bookings.filter(row=>status==="all"||row.status===status);
  if (!entries.length) queue.append(make("p","muted","此日期区间无匹配预约。"));
  for(const item of entries) {
    const row = bookingRow(item);
    const controls = row.lastElementChild;
    controls.replaceChildren();
    const modes=item.status==="pending"?[["approve","通过"],["reject","拒绝"]]:
                 item.status==="confirmed"?[["cancel","取消"]]:[];
    for(const [action,label] of modes) controls.append(button(label,async()=>{
      await api("/api/admin/bookings/"+item.id,"POST",{action});
      await refreshAdmin();
      notice("预约状态已更新。");
    }));
    queue.append(row);
  }
}
async function refreshAdmin() {
  const query=range();
  const [rooms,bookings,closures,report,audit]=await Promise.all([
    api("/api/admin/rooms"),api("/api/admin/bookings?"+query),api("/api/admin/closures?"+query),
    api("/api/admin/report?"+query),api("/api/admin/audit"),
  ]);
  adminData = {rooms,bookings,closures,report,audit};
  const sums = key => report.rooms.reduce((acc,room)=>acc+room[key],0);
  $("metrics").replaceChildren(...[["预约总数",sums("total")],["待审批",sums("pending")],
    ["爽约",sums("no_show")],["签到场次预约时长",sums("allocated_minutes")+" 分钟"]].map(([name,value])=>{
      const card=make("div","metric");
      card.append(make("b",null,value),make("small",null,name));
      return card;
    }));
  $("room-report").replaceChildren(...report.rooms.map(room=>{
    const row=make("tr");
    const name=make("th",null,room.name);
    name.scope="row";
    row.append(name,...[room.total,room.pending,room.cancelled,room.no_show,room.allocated_minutes+" 分钟"]
      .map(value=>make("td",null,value)));
    return row;
  }));
  $("manage-rooms").replaceChildren(...rooms.map(room=>{
    const row=make("div","row");
    row.append(make("span",null,[room.name,room.area,room.active?"开放":"停用"].join(" · ")),button("编辑 "+room.name,()=>editRoom(room)));
    return row;
  }));
  fillOptions($("closure-room"),rooms.filter(room=>room.active).map(room=>[room.id,room.name]),$("closure-room").value);
  $("closures").replaceChildren(...closures.map(item=>{
    const row=make("div","row");
    row.append(make("span",null,[item.day,item.room,hm(item.start)+"–"+hm(item.end),item.reason].join(" · ")),
      button("解除封闭",async()=>{
        await api("/api/admin/closures/"+item.id,"DELETE");
        await refreshAdmin();
        notice("已解除封闭时段。");
      }));
    return row;
  }));
  if(!closures.length) $("closures").append(make("p","muted","此日期区间无封闭计划。"));
  $("audit").replaceChildren(...audit.map(item=>{
    const row=make("div","row");
    row.append(make("span",null,[item.actor,actionsChinese[item.action]||item.action,item.detail].filter(Boolean).join(" · ")),
      make("small",null,item.at.slice(0,19).replace("T"," ")));
    return row;
  }));
  if(!audit.length) $("audit").append(make("p","muted","暂无操作记录。"));
  adminQueue();
}
async function show(which) {
  $("student").hidden=which!=="student"; $("admin").hidden=which!=="admin";
  $("student-tab").classList.toggle("active",which==="student");
  $("admin-tab").classList.toggle("active",which==="admin");
  notice("");
  if(which==="student") {
    await ensureStudent();
    await refreshStudent();
  } else {
    const enabled = session.role==="admin";
    $("admin-login").hidden=enabled; $("admin-body").hidden=!enabled; $("logout").hidden=!enabled;
    if(enabled) await refreshAdmin();
    else if(!session.admin_enabled) notice("管理员功能尚未开启。请设置启动口令后重启。");
  }
}
function bindForm(id, handler) {
  $(id).addEventListener("submit",event=>{
    event.preventDefault();
    run(async()=>{
      const submit=event.submitter;
      if(submit) submit.disabled=true;
      try { await handler(); } finally { if(submit && id!=="booking-form") submit.disabled=false; }
    });
  });
}

$("day").value=$("day").min=today; $("day").max=dayAfter(today,6);
$("report-first").value=today; $("report-last").value=dayAfter(today,6); $("closure-day").value=dayAfter(today,1);
$("day").addEventListener("change",()=>run(refreshStudent));
$("room-filter").addEventListener("change",()=>{if(studentDay)renderRooms();});
$("room").addEventListener("change",()=>renderStart());
$("start").addEventListener("change",renderEnd);
for(const tab of document.querySelectorAll("[data-view]")) tab.addEventListener("click",()=>run(async()=>{
  mode=tab.dataset.view;
  for(const other of document.querySelectorAll("[data-view]")) other.classList.toggle("active",other===tab);
  await refreshStudent();
}));
$("student-tab").addEventListener("click",()=>run(()=>show("student")));
$("admin-tab").addEventListener("click",()=>run(()=>show("admin")));
$("refresh-mine").addEventListener("click",()=>run(refreshStudent));
$("admin-refresh").addEventListener("click",()=>run(refreshAdmin));
$("queue-filter").addEventListener("change",()=>{if(adminData)adminQueue();});
$("csv").addEventListener("click",()=>{
  const link=make("a");
  link.href="/api/admin/report?"+range()+"&format=csv";
  link.download="room-report.csv";
  link.click();
});
$("logout").addEventListener("click",()=>run(async()=>{
  await api("/api/session/logout","POST");
  session=await api("/api/session");
  await show("student");
}));
bindForm("booking-form",async()=>{
  const n=Number($("days").value);
  try {
    const result=await api("/api/bookings","POST",{room:$("room").value,day:$("day").value,
      start:Number($("start").value),end:Number($("end").value),title:$("title").value,
      attendees:Number($("people").value),occurrences:n,repeat:n>1?"daily":"once"});
    $("title").value="";
    notice(result.status==="pending"?"已提交，等待管理员审批。":"预约已确认。");
  } catch(error) {
    notice(error.message,true);
  }
  await refreshStudent();
});
bindForm("login-form",async()=>{
  session={...session,...await api("/api/session/admin","POST",{password:$("password").value})};
  $("password").value="";
  await show("admin");
});
bindForm("room-form",async()=>{
  await api("/api/admin/rooms/"+encodeURIComponent($("edit-id").value.trim()),"PUT",
    {name:$("edit-name").value,area:$("edit-area").value,capacity:Number($("edit-cap").value),
     equipment:$("edit-equip").value,active:$("edit-active").checked,approval:$("edit-approval").checked});
  await refreshAdmin();
  notice("房间配置已保存。");
});
bindForm("closure-form",async()=>{
  await api("/api/admin/closures","POST",{room:$("closure-room").value,day:$("closure-day").value,
    start:timeValue($("closure-start").value),end:timeValue($("closure-end").value),reason:$("closure-reason").value});
  $("closure-reason").value="";
  await refreshAdmin();
  notice("已封闭所选时段。");
});
run(async()=>{
  session=await api("/api/session");
  await show(session.role==="admin"?"admin":"student");
});

