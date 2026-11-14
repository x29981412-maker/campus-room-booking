# 高校研讨空间预约原型

面向上海高校小组讨论场景的本地可运行原型。它覆盖查空位、按容量选房、提交预约和取消释放时段，适合演示如何把线下空间规则做成可用的软件。

![预约完成后，时段和本浏览器的预约同时更新](docs/demo.png)

## 来源和实现

- [上海财经大学图书馆学习共享空间](https://lib.sufe.edu.cn/07/3d/c8286a132925/page.htm)提供业务案例。公开说明中的提前七天（含当天）、至少三人和单次限定四小时被写入原型。
- [Meeting Room Booking System](https://github.com/meeting-room-booking-system/mrbs-code)用作预约流程和房间日历的设计参考。
- 项目用 Python、FastAPI、SQLite 和原生浏览器组件实现。房间、开放时段（09:00–21:00）、半小时档位及预约凭证是演示设定。演示没有校方账号或真实房间数据。

预约时在同一个 SQLite 写事务里检查时段重叠并创建记录。相邻的两个时段可以分别预约。取消需要创建时返回的凭证，成功后立即释放原时段。浏览器保留本机发起的预约，刷新可继续取消。

## 运行

Windows 上验证使用 Python 3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:create_app --factory --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 。数据保存在 `runtime/rooms.sqlite`，可以通过 `ROOM_BOOKING_DB` 更改路径。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

三项测试覆盖日期、人数和时长限制，重叠冲突、取消后的重新预约，以及两个并发请求争同一时段只成功一个。在浏览器中实际验证查空位、创建预约、刷新后取消并释放时段。

