"use strict";
const $ = id => document.getElementById(id);
const day = $("day");
const room = $("room");
const start = $("start");
const end = $("end");
const submit = $("submit");
const tz = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"
});
const limit = new Date(Date.now() + 6 * 86400000);
const fmt = minute => String(Math.floor(minute / 60)).padStart(2,"0") +
  ":" + String(minute % 60).padStart(2,"0");
let data = null;
let generation = 0;
let bookings = [];
try { bookings = JSON.parse(localStorage.getItem("study-room-bookings") || "[]"); } catch {}
if (!Array.isArray(bookings)) bookings = [];
day.value = day.min = tz.format(new Date());
day.max = tz.format(limit);

async function api(path, data) {
  const options = data ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)} : {};
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "请检查所填信息。");
  return payload;
}

function option(value, text) { return new Option(text, String(value)); }
function feedback(text, error=false) {
  $("feedback").textContent = text;
  $("feedback").dataset.error = String(error);
}

function renderSlots() {
  const view = $("rooms");
  view.replaceChildren();
  const selected = room.value;
  room.replaceChildren();
  for (const item of data.rooms) {
    room.append(option(item.id,item.name));
    const section = document.createElement("article");
    section.className = "room";
    const title = document.createElement("strong");
    title.textContent = item.name + " · " + item.capacity + "人 · " + item.equipment;
    const times = document.createElement("div");
    times.className = "slots";
    for (let time=data.open; time<data.close; time+=data.step) {
      const cell = document.createElement("span");
      const busy = item.reserved.some(([a,b]) => a < time + data.step && b > time);
      cell.className = busy ? "busy" : time < data.not_before ? "past" : "";
      cell.textContent = fmt(time) + (busy ? " 已约" : cell.className ? " 已过" : " 空闲");
      times.append(cell);
    }
    section.append(title, times);
    view.append(section);
  }
  if (data.rooms.some(item => item.id === selected)) room.value = selected;
  updateStart();
}

function updateStart() {
  const picked = data.rooms.find(item => item.id === room.value);
  start.replaceChildren();
  for (let time=data.open; time<data.close; time+=data.step) {
    if (time >= data.not_before && !picked.reserved.some(([a,b]) => a < time+data.step && b > time)) {
      start.append(option(time,fmt(time)));
    }
  }
  $("attendees").max = String(picked.capacity);
  updateEnd();
}
function updateEnd() {
  const picked = data.rooms.find(item => item.id === room.value);
  end.replaceChildren();
  if (start.value !== "") {
    const lower = Number(start.value);
    for (let time=lower+data.step; time<=Math.min(lower+240,data.close); time+=data.step) {
      if (picked.reserved.some(([a,b]) => a < time && b > lower)) break;
      end.append(option(time,fmt(time)));
    }
  }
  submit.disabled = !end.value;
}

async function refresh() {
  const query = ++generation;
  submit.disabled = true;
  try {
    const payload = await api("/api/rooms?day=" + encodeURIComponent(day.value));
    if (query !== generation) return;
    data = payload;
    renderSlots();
  } catch (error) {
    if (query === generation) {
      $("rooms").replaceChildren();
      feedback(error.message,true);
    }
  }
}
function save() { localStorage.setItem("study-room-bookings",JSON.stringify(bookings)); }
function renderBookings() {
  const list = $("active");
  list.replaceChildren();
  if (!bookings.length) {
    const row = document.createElement("li");
    row.textContent = "暂无预约";
    list.append(row);
  }
  for (const item of bookings) {
    const row = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = item.title;
    const detail = document.createElement("p");
    detail.textContent = item.day + " · 研讨室 " + item.room + " · " + fmt(item.start) + "–" + fmt(item.end);
    const button = document.createElement("button");
    button.textContent = "取消预约";
    button.className = "secondary";
    button.addEventListener("click",async () => {
      button.disabled = true;
      try {
        await api("/api/bookings/cancel",{token:item.token});
        bookings = bookings.filter(entry => entry.token !== item.token);
        save();
        renderBookings();
        feedback("已取消预约，时段已释放。");
        await refresh();
      } catch (error) { feedback(error.message,true); button.disabled = false; }
    });
    row.append(title,detail,button);
    list.append(row);
  }
}
day.addEventListener("change",refresh);
room.addEventListener("change",updateStart);
start.addEventListener("change",updateEnd);
$("book").addEventListener("submit",async event => {
  event.preventDefault();
  submit.disabled = true;
  try {
    const booked = await api("/api/bookings",{
      room:room.value,day:day.value,start:Number(start.value),end:Number(end.value),
      attendees:Number($("attendees").value),title:$("title").value
    });
    bookings.unshift(booked);
    save();
    renderBookings();
    $("title").value = "";
    feedback("预约已确认。");
  } catch (error) { feedback(error.message,true); }
  await refresh();
});
renderBookings();
refresh();

