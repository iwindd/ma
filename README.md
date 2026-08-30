# Neural Scroll — Custom Command Deck

UI แบบ Cyber Control Deck สำหรับ scroll macro เดิม พร้อม global key/mouse trigger, Toggle/Hold mode, ปรับ cadence/intensity, live telemetry และ event log

## เริ่มใช้งาน

```bash
python -m pip install -r requirements.txt
python macro_script.py
```

> Tkinter มากับ Python บน Windows/macOS โดยทั่วไป หาก Linux ไม่มี ให้ติดตั้งแพ็กเกจ Tk ของระบบก่อน (เช่น `python3-tk`)

## Controls

- **ARM SYSTEM** — เปิด/ปิด macro จาก UI เมื่อใช้ Toggle mode; ใน Hold mode ต้องกด trigger ค้าง (ปุ่ม UI ใช้ force-disarm ได้)
- **Trigger** — เลือก `F`, `G`, `Space`, `Shift`, `Ctrl`, Middle Mouse, Mouse 4 หรือ Mouse 5
- **Toggle** — กด trigger หนึ่งครั้งเพื่อเปิด และกดอีกครั้งเพื่อปิด
- **Hold** — macro ทำงานเฉพาะตอนกด trigger ค้าง
- **Cadence** — ระยะพักระหว่าง pulse (ค่าน้อยทำงานถี่กว่า)
- **Intensity** — จำนวน scroll steps ต่อ pulse

การเปลี่ยน Trigger หรือ Mode จะ disarm ระบบอัตโนมัติเพื่อป้องกันการทำงานโดยไม่ตั้งใจ

## หมายเหตุระบบ

- แอปใช้ global input hooks และส่ง mouse wheel events ไปยังหน้าต่างที่กำลังรับอินพุต
- macOS อาจต้องเปิด Accessibility/Input Monitoring permission
- Linux ต้องมี graphical session ที่ `pynput` รองรับ; Wayland บางระบบอาจจำกัด global hooks
- ปิดแอปด้วยปุ่ม `×` เพื่อหยุด worker และ input listeners อย่างถูกต้อง
- ควรตรวจสอบกฎของแอปหรือเกมเป้าหมายก่อนใช้ automation

## Architecture

`MacroEngine` แยกจาก `MacroApp`: engine ดูแล state, debounce, listeners, cancellable worker และ thread safety ส่วน UI รับ event ผ่าน queue เพื่อให้การอัปเดต Tkinter เกิดบน main thread เท่านั้น ไม่มีการติดตั้ง dependency ตอน runtime หรือ prompt ตอน import
