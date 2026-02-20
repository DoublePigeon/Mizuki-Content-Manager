import gradio as gr
import os
import json
import re
import shutil
import frontmatter
import json5  # 用于解析带注释和单引号的TS数据
import platform
from datetime import datetime, date, time
from PIL import Image
from matplotlib import colors

# ====================
# 配置与接口加载
# ====================

def load_interfaces():
    try:
        with open("interfaces.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("警告: 找不到 interfaces.json，将使用空定义。")
        return {}

INTERFACES = load_interfaces()

def get_allowed_paths():
    """获取系统盘符以授权Gradio访问任意文件"""
    drives = []
    if platform.system() == "Windows":
        import string
        from ctypes import windll
        bitmask = windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drives.append(f"{letter}:\\")
            bitmask >>= 1
    else:
        drives.append("/")
    return drives

def convert_to_jpg(input_path:str, quality=95):
    """转换图像，直接储存在同一目录下"""
    try:
        # 打开图片
        with Image.open(input_path) as img:
            if input_path.endswith(".jpg"):
                print(f"Stopped converting for its already being JPEG picture")
                return
            # 如果图片有透明通道 (RGBA) 或 调色板模式 (P)
            if img.mode in ("RGBA", "P"):
                # 创建一个白色背景的底图，大小与原图一致
                background = Image.new("RGB", img.size, (255, 255, 255))
                # 将原图粘贴到底图上，使用原图的 alpha 通道作为掩码
                background.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
                img = background
            else:
                # 如果是其他模式（如 CMYK 或 RGBA 转换过来的 RGB），统一转为 RGB
                img = img.convert("RGB")
            
            # 保存为 JPG
            output_path = os.path.splitext(input_path)[0] + ".jpg"
            img.save(output_path, "JPEG", quality=quality)
            try:
                os.remove(input_path)
            except Exception as e:
                print(f"无法删除原文件 {input_path}: {e}")
            print(f"Converted to jpg: {output_path}")
            
    except Exception as e:
        print(f"Convertion failed {input_path}: {e}")

# ====================
# 核心解析逻辑 (关键修复)
# ====================

def extract_ts_variable_value(content, var_name):
    """
    一个简单的词法分析器，用于提取 'export const var = [...]' 中的 [...] 部分。
    支持处理嵌套括号、跳过字符串内部和注释，从而提取出完整的 JSON5 字符串。
    """
    # 1. 定位变量定义开始
    pattern = rf"export const {var_name}\s*(?::\s*[\w\[\]\|\s<>\.]+)?\s*=\s*"
    match = re.search(pattern, content)
    if not match:
        return None, content

    start_idx = match.end()
    
    # 2. 寻找第一个由 [ 或 { 开始的数据块
    cursor = start_idx
    while cursor < len(content) and content[cursor] not in ['[', '{']:
        cursor += 1
    
    if cursor >= len(content): return None, content

    # 3. 括号平衡算法 (State Machine)
    stack = []
    in_string = False
    string_char = None
    in_comment = False # //
    in_block_comment = False # /* */
    
    i = cursor
    while i < len(content):
        char = content[i]
        prev_char = content[i-1] if i > 0 else ''
        next_char = content[i+1] if i < len(content)-1 else ''

        # 处理注释 (不在字符串内时)
        if not in_string:
            if not in_comment and not in_block_comment:
                if char == '/' and next_char == '/':
                    in_comment = True
                    i += 2
                    continue
                if char == '/' and next_char == '*':
                    in_block_comment = True
                    i += 2
                    continue
            
            if in_comment:
                if char == '\n':
                    in_comment = False
                i += 1
                continue
            
            if in_block_comment:
                if char == '*' and next_char == '/':
                    in_block_comment = False
                    i += 2
                else:
                    i += 1
                continue

        # 处理字符串 (不在注释内时)
        if not in_comment and not in_block_comment:
            if char in ['"', "'", '`']:
                if not in_string:
                    in_string = True
                    string_char = char
                elif char == string_char:
                    # 简单的转义检查: 偶数个反斜杠表示非转义，但这里简化检查前一个是否为 \
                    # 为了更严谨，应该回溯检查连续 \ 的数量，但在规范的TS中通常 prev_char != '\' 足够
                    if prev_char != '\\': 
                        in_string = False
                        string_char = None
        
        # 处理括号 (不在字符串和注释内时)
        if not in_string and not in_comment and not in_block_comment:
            if char in ['[', '{']:
                stack.append(char)
            elif char in [']', '}']:
                if stack:
                    stack.pop()
                    if not stack:
                        # 栈空了，说明找到了匹配的结束括号
                        data_str = content[cursor:i+1]
                        tail_code = content[i+1:]
                        return data_str, tail_code
        
        i += 1
        
    return None, content

def parse_ts_data(root, filename, var_name):
    """读取并解析 TS 文件"""
    path = os.path.join(root, "src", "data", filename)
    if not os.path.exists(path):
        return [], ""
    
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    data_str, tail_code = extract_ts_variable_value(content, var_name)
    
    if data_str:
        try:
            # 使用 json5 解析，它完美支持 TS 中的单引号、无引号Key、尾随逗号、注释等
            data = json5.loads(data_str)
            return data, tail_code
        except Exception as e:
            print(f"JSON5 解析 {filename} 失败: {e}")
            return [], ""
    else:
        print(f"未能提取 {filename} 中的变量 {var_name}")
        return [], ""

def write_ts_data(root, filename, var_name, data, interface_key, tail_code):
    """回写 TS 文件"""
    path = os.path.join(root, "src", "data", filename)
    if not path: return "路径错误"
    
    # 写入时使用标准 JSON 格式 (TS 兼容)
    json_str = json.dumps(data, indent=4, ensure_ascii=False)
    
    interface_str = INTERFACES.get(interface_key, "")
    ts_type = get_ts_type(interface_key)
    
    if not tail_code: tail_code = ";\n"
    # 确保 tail_code 以分号开头（因为解析器截取时可能不含分号）
    if not tail_code.strip().startswith(";"):
        tail_code = ";\n" + tail_code

    new_content = f"{interface_str}\nexport const {var_name}: {ts_type} = {json_str}{tail_code}"
    
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return f"成功保存 {filename}"
    except Exception as e:
        return f"保存失败: {str(e)}"

def get_ts_type(key):
    mapping = {
        "diary": "DiaryItem[]",
        "friends": "FriendItem[]",
        "projects": "Project[]",
        "timeline": "TimelineItem[]",
        "skills": "Skill[]",
        "devices": "DeviceCategory"
    }
    return mapping.get(key, "any")

def get_path(root, *args):
    if not root: return None
    return os.path.join(root, *args)

# ====================
# 业务逻辑
# ====================

# --- Diary ---
def save_img_to_diary(root, imgs) -> list:
    dest_dir = get_path(root, "public", "images", "diary")
    img_paths = []
    for img in imgs:
        name, suffix = os.path.splitext(os.path.basename(img.name))
        dest = os.path.join(dest_dir, f"{name}{suffix}")
        i = 0
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{name}_{i}{suffix}")

        shutil.copy(img.name, dest)
        img_paths.append(f"/images/diary/{os.path.basename(dest)}")

    return img_paths

def load_diary_ui(root):
    data, tail = parse_ts_data(root, "diary.ts", "diaryData")
    if not data: data = []
    choices = [f"{item['id']} - {item['date']}" for item in data]
    
    # 获取图片
    path = get_path(root, "public", "images", "diary")
    imgs = []
    if path and os.path.exists(path):
        for f in os.listdir(path):
            if f.lower().endswith(('.jpg', '.png', '.webp')):
                imgs.append(f"/images/diary/{f}")
                
    return gr.Dropdown(choices=choices, value=None), data, tail

def select_diary(val, data):
    if not val or not data: return (None, "", "", "", "")
    try:
        did = int(val.split(' - ')[0])
        item = next((x for x in data if x['id'] == did), None)
        if not item: return (None, "", "", "", "")
        return (
            item['id'],
            item.get('content', ''),
            item.get('mood', ''),
            item.get('location', ''),
            ",".join(item.get('tags', [])),
        )
    except: return (None, "", "", "", "")

def save_diary_entry(root, data, tail, d_id, content, mood, loc, tags, imgs: list):
    if not data: data = []
    target = next((x for x in data if x['id'] == d_id), None)
    diary_pic_path = get_path(root, "public", "images", "diary")
    img_paths = []
    if imgs:
        for img in imgs:
            img_name, suffix = os.path.split(os.path.basename(img.name))
            dest_path = os.path.join(diary_pic_path, f"{img_name}{suffix}")
            i = 0
            while os.path.exists(dest_path):
                dest_path = os.path.join(diary_pic_path, f"{img_name}_{i}{suffix}")
                i += 1

            shutil.move(img.name, dest_path)
            img_paths.append(f"/images/diary/{os.path.basename(dest_path)}")
            #白写了没蚌住

    new_item = {
        "id": d_id,
        "content": content,
        "date": target['date'] if target else datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "images": img_paths,
        "location": loc,
        "mood": mood,
        "tags": [t.strip() for t in tags.split(',') if t.strip()]
    }
    if target:
        data[data.index(target)] = new_item
        msg = write_ts_data(root, "diary.ts", "diaryData", data, "diary", tail)
        u = load_diary_ui(root)
        return msg, u[0], u[1], u[2]
    else:
        return "未选中有效的日记"

def create_diary_entry(root, content, mood, loc, tags, imgs):
    data, tail = parse_ts_data(root, "diary.ts", "diaryData")
    if not data: data = []
    new_id = max([x['id'] for x in data], default=0) + 1
    if len(imgs) > 0:
        img_paths = save_img_to_diary(root, imgs)
    new_item = {
        "id": new_id,
        "content": content,
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "images": img_paths if img_paths else [],
        "location": loc,
        "mood": mood,
        "tags": [t.strip() for t in tags.split(',') if t.strip()]
    }
    data.append(new_item)
    msg = write_ts_data(root, "diary.ts", "diaryData", data, "diary", tail)
    u = load_diary_ui(root)
    return msg, u[0], u[1], u[2]

def delete_selected_diary(root, data, tail, d_id):
    print(f"Passing following args to delete_selected_diary:\n\
{root} -> root\n\
{data} -> data\n\
{tail} -> tail\n\
{d_id} -> d_id\n\
")
    if not d_id:
        return "未选中有效的日记", gr.update(), gr.update(), gr.update()
    
    found = False
    for item in data:
        if item["id"] == d_id:
            data.remove(item)
            found = True
            break
    
    if not found:
        return "未找到要删除的日记", gr.update(), gr.update(), gr.update()
    write_ts_data(root, "diary.ts", "diaryData", data, "diary", tail)
    u = load_diary_ui(root)
    return "删除了ID为{d_id}的日记", u[0], u[1], u[2]
    
# --- Friends ---
def load_friends_ui(root):
    data, tail = parse_ts_data(root, "friends.ts", "friendsData")
    if not data: data = []
    choices = [f"{item['id']} - {item['title']}" for item in data]
    return gr.Dropdown(choices=choices, value=None), data, tail

def select_friend(val, data):
    if not val or not data: return (None, "", "", "", "", "")
    try:
        fid = int(val.split(' - ')[0])
        item = next((x for x in data if x['id'] == fid), None)
        return item['id'], item['title'], item['imgurl'], item['desc'], item['siteurl'], ",".join(item.get('tags', []))
    except: return (None, "", "", "", "", "")

def save_friend_btn(root, data, tail, fid, title, img, desc, site, tags):
    if not data: data = []
    item = next((x for x in data if x['id'] == fid), None)
    if item:
        item.update({
            "title": title, "imgurl": img, "desc": desc, "siteurl": site,
            "tags": [t.strip() for t in tags.split(',') if t.strip()]
        })
        msg = write_ts_data(root, "friends.ts", "friendsData", data, "friends", tail)
        u = load_friends_ui(root)
        return msg, u[0], u[1], u[2]
    return "Error", gr.Dropdown(), data, tail

def create_friend_btn(root, title, img, desc, site, tags):
    data, tail = parse_ts_data(root, "friends.ts", "friendsData")
    if not data: data = []
    new_id = max([x['id'] for x in data], default=0) + 1
    data.append({
        "id": new_id, "title": title, "imgurl": img, "desc": desc, "siteurl": site,
        "tags": [t.strip() for t in tags.split(',') if t.strip()]
    })
    msg = write_ts_data(root, "friends.ts", "friendsData", data, "friends", tail)
    u = load_friends_ui(root)
    return msg, u[0], u[1], u[2]

def delete_selected_friend(root, data, tail, fid):
    print(f"Passing following args to delete_selected_friend:\n\
{root} -> root\n\
{data} -> data\n\
{tail} -> tail\n\
{fid} -> fid\n\
")
    if not fid:
        return "未选中有效的友情链接", gr.update(), gr.update(), gr.update()
    
    found = False
    for item in data:
        if item["id"] == fid:
            data.remove(item)
            found = True
            break
    
    if not found:
        return "未找到要删除的友情链接", gr.update(), gr.update(), gr.update()
    write_ts_data(root, "friends.ts", "friendsData", data, "friends", tail)
    u = load_friends_ui(root)
    return f"删除了ID为{fid}的友情链接", u[0], u[1], u[2]

# --- Projects ---
def load_projects_ui(root):
    data, tail = parse_ts_data(root, "projects.ts", "projectsData")
    if not data: data = []
    choices = [f"{p['id']} - {p['title']}" for p in data]
    return gr.Dropdown(choices=choices, value=None), data, tail

def select_project(val, data):
    empty = (None, "", "", "", None, "", None, "", "", "", "", False, "", "")
    if not val or not data: return empty
    try:
        pid = val.split(' - ')[0]
        item = next((x for x in data if x['id'] == pid), None)
        if not item: return empty
        return (
            item['id'], item['title'], item['description'], item['image'], 
            item['category'], ",".join(item.get('techStack', [])), item['status'],
            item.get('liveDemo', ''), item.get('sourceCode', ''), 
            item.get('startDate', ''), item.get('endDate', ''), item.get('featured', False),
            ",".join(item.get('tags', [])), item.get('visitUrl', '')
        )
    except: return empty

def save_project_all(root, data, tail, *args):
    (pid, title, desc, img, cat, stack, status, demo, code, start, end, feat, tags, visit) = args
    print(f"Passed following args to save_project_all:\n\
{root} -> root\n\
{data} -> data\n\
{tail} -> tail\n\
{pid} -> pid\n\
{title} -> title\n\
{desc} -> desc\n\
{img} -> img\n\
{cat} -> cat\n\
{stack} -> stack\n\
{status} -> status\n\
{demo} -> demo\n\
{code} -> code\n\
{start} -> start\n\
{end} -> end\n\
{feat} -> feat\n\
{tags} -> tags\n\
{visit} -> visit\n\
")
    if not data: data = []
    new_obj = {
        "id": pid, "title": title, "description": desc, "image": img,
        "category": cat, "techStack": [t.strip() for t in stack.split(',') if t.strip()],
        "status": status, "liveDemo": demo, "sourceCode": code,
        "startDate": start, "endDate": end, "featured": feat,
        "tags": [t.strip() for t in tags.split(',') if t.strip()], "visitUrl": visit
    }

    is_new = True
    for item in data:
        if item["id"] == pid:
            is_new = False

    if is_new:
        data.append(new_obj)
    else:
        found = False
        for i, item in enumerate(data):
            if item['id'] == pid:
                data[i] = new_obj
                found = True
                break
        if not found: data.append(new_obj)
    msg = write_ts_data(root, "projects.ts", "projectsData", data, "projects", tail)
    u = load_projects_ui(root)
    return msg, u[0], u[1], u[2]

def delete_selected_project(root, data, tail, pid):
    print(f"Passing following args to delete_selected_project:\n\
{root} -> root\n\
{data} -> data\n\
{tail} -> tail\n\
{pid} -> pid\n\
")
    if not pid:
        return "未选中有效的项目", gr.update(), gr.update(), gr.update()
    
    found = False
    for item in data:
        if item["id"] == pid:
            data.remove(item)
            found = True
            break

    if not found:
        return "未找到要删除的项目", gr.update(), gr.update(), gr.update()
    write_ts_data(root, "projects.ts", "projectsData", data, "projects", tail)
    u = load_projects_ui(root)
    return f"删除了ID为{pid}的项目", u[0], u[1], u[2]

# --- Timeline ---

def load_timeline_ui(root):
    data, tail = parse_ts_data(root, "timeline.ts", "timelineData")
    if not data: data = []
    choices = [f"{item['id']} - {item['title']}" for item in data]
    return gr.Dropdown(choices=choices, value=None), data, tail

def select_timeline(val, data):
    empty = ("", "", "", "", "", "", "", "", False)
    if not val or not data:
        return empty
    try:
        tiid = val.split(' - ')[0]
        for item in data:
            if item['id'] == tiid:
                return (item['id'], item['title'], item['description'], item['type'],
                        item['startDate'], item.get('location', ''), item.get('organization', ''),
                        ",".join(item.get('skills', [])), item.get('featured', False), item.get('color', '')
                        )
            
        return empty
    except:
        return empty
    
def save_timeline(root, data, tail, tiid, title, desc, titype, date, loc="", org="", skills="", feat=False):
    print(f"Passing following args to save_timeline:\n\
{root} -> root\n\
{data} -> data\n\
{tail[:100]}... -> tail\n\
{tiid} -> tiid\n\
{title} -> title\n\
{desc} -> desc\n\
{titype} -> type\n\
{date} -> date\n\
{loc} -> loc\n\
{org} -> org\n\
{skills} -> skills\n\
{feat} -> feat\n\
")
    if not tiid or not title or not desc or not date or not titype:
        return "必须填入ID、标题、描述、日期和类型", gr.update(), gr.update(), gr.update()
    if not data:
        data = []
    new_obj = {
        "id": tiid, "title": title, "description": desc,
        "startDate": date, "location": loc, "organization": org,
        "skills": [s.strip() for s in skills.split(',') if s.strip()], "featured": feat
    }
    if titype == "教育":
        new_obj["type"] = "education"
        new_obj["icon"] = "material-symbols:school"
        new_obj["color"] = "#2A53DD"
    elif titype == "证书":
        new_obj["type"] = "certificate"
        new_obj["icon"] = "material-symbols:lab-profile"
        new_obj["color"] = "#51F56C"
    elif titype == "项目":
        new_obj["type"] = "project"
        new_obj["icon"] = "material-symbols:code-blocks"
        new_obj["color"] = "#FF4B63"
    elif titype == "其它":
        new_obj["type"] = "other"
        new_obj["icon"] = "material-symbols:editor-choice"
        new_obj["color"] = "#FAB83E"

    is_new = True
    for item in data:
        if item["id"] == tiid:
            is_new = False

    if is_new:
        data.append(new_obj)
    else:
        for i, item in enumerate(data):
            if item['id'] == tiid:
                cat = item.get('type', '')
                if not titype:
                    new_obj['type'] = cat
                    if cat == "education":
                        new_obj["icon"] = "material-symbols:school"
                        new_obj["color"] = "#2A53DD"
                    elif cat == "certificate":
                        new_obj["icon"] = "material-symbols:lab-profile"
                        new_obj["color"] = "#51F56C"
                    elif cat == "project":
                        new_obj["icon"] = "material-symbols:code-blocks"
                        new_obj["color"] = "#FF4B63"
                    elif cat == "other":
                        new_obj["icon"] = "material-symbols:editor-choice"
                        new_obj["color"] = "#FAB83E"
                data[i] = new_obj
                break
    
    write_ts_data(root, "timeline.ts", "timelineData", data, "timeline", tail)
    u = load_timeline_ui(root)
    return f"已保存ID为{tiid}的时间线事件", u[0], u[1], u[2]

def delete_selected_timeline(root, data, tail, tiid):
    print(f"Passing following args to delete_selected_timeline:\n\
{root} -> root\n\
{data} -> data\n\
{tail} -> tail\n\
{tiid} -> tiid\n\
")
    if not tiid:
        return "未选中有效的时间线事件", gr.update(), gr.update(), gr.update()
    
    found = False
    for item in data:
        if item["id"] == tiid:
            data.remove(item)
            found = True
            break

    if not found:
        return "未找到要删除的时间线事件", gr.update(), gr.update(), gr.update()

    write_ts_data(root, "timeline.ts", "timelineData", data, "timeline", tail)
    u = load_timeline_ui(root)
    return f"删除了ID为{tiid}的时间线事件", u[0], u[1], u[2]

# --- Skills ---

def load_skills_ui(root):
    data, tail = parse_ts_data(root, "skills.ts", "skillsData")
    if not data: data = []
    choices = [f"{item['id']} - {item['name']}" for item in data]
    return gr.Dropdown(choices=choices, value=None), data, tail

def select_skill(val, data):
    empty = (None, "", "", "", "", "", None, None, "")
    if not val or not data: return empty
    try:
        sid = val.split(' - ')[0]
        for item in data:
            if item['id'] == sid:
                print(f"Found valid skill item")
                return (item['id'], item['name'], item['description'], item['icon'],
                        item['category'], item['level'], item['experience']['years'], item['experience']['months'], item.get("color", "")
                        )
    except: return empty

def save_skill(root, data, tail, sid, name, desc, icon, cat, level, exp_yr, exp_mo, color):
    print(f"Passing following args to save_skill:\n\
{root} -> root\n\
{data} -> data\n\
{tail} -> tail\n\
{sid} -> sid\n\
{name} -> name\n\
{desc} -> desc\n\
{icon} -> icon\n\
{cat} -> cat\n\
{level} -> level\n\
{exp_yr} -> exp_yr\n\
{exp_mo} -> exp_mo\n\
{color} -> color\n\
")
    if not exp_yr:
        exp_yr = 0
    if not exp_mo:
        exp_mo = 0
    if not icon:
        icon = "material-symbols:construction-rounded"
    if not sid or not name or not desc or not icon or not cat or not level:
        return "必须填入ID、名称、描述、图标、分类、熟练度和经验长度", gr.update(), gr.update(), gr.update()
    if not data:
        data = []

    new_obj = {
        "id": sid, "name": name, "description": desc, "icon": icon,
        "category": cat, "level": level, "experience": {"years": int(exp_yr), "months": int(exp_mo)},
    }
    if color:
        new_obj["color"] = color

    is_new = True
    for item in data:
        if item["id"] == sid:
            is_new = False

    if is_new:
        data.append(new_obj)
    else:
        for i, item in enumerate(data):
            if item['id'] == sid:
                if not cat:
                    new_obj['category'] = item["category"]
                if not level:
                    new_obj['level'] = item["level"]
                data[i] = new_obj
                break
    
    write_ts_data(root, "skills.ts", "skillsData", data, "skills", tail)
    u = load_skills_ui(root)
    return f"已保存ID为{sid}的技能", u[0], u[1], u[2]

def delete_selected_skill(root, data, tail, sid):
    print(f"Passing following args to delete_selected_skill:\n\
{root} -> root\n\
{data} -> data\n\
{tail} -> tail\n\
{sid} -> sid\n\
")
    if not sid:
        return "未选中有效的技能", gr.update(), gr.update(), gr.update()
    
    found = False
    for item in data:
        if item["id"] == sid:
            data.remove(item)
            found = True
            break
    
    if not found:
        return "未找到要删除的技能", gr.update(), gr.update(), gr.update()
    
    write_ts_data(root, "skills.ts", "skillsData", data, "skills", tail)
    u = load_skills_ui(root)
    return f"删除了ID为{sid}的技能", u[0], u[1], u[2]
    

# --- Devices ---
def load_devices_ui(root):
    data, tail = parse_ts_data(root, "devices.ts", "devicesData")
    if not data: data = {}
    choices = []
    flat_map = {}
    if isinstance(data, dict):
        for cat, items in data.items():
            for idx, item in enumerate(items):
                label = f"{cat} - {item['name']}"
                choices.append(label)
                flat_map[label] = (cat, idx)
    return gr.Dropdown(choices=choices, value=None), data, tail, flat_map

def select_device(val, data, flat_map):
    empty = ("", "", "", "", "", "")
    if not val or not flat_map: return empty
    try:
        cat, idx = flat_map[val]
        item = data[cat][idx]
        return cat, item['name'], item['image'], item['specs'], item['description'], item['link']
    except: return empty

def save_device_btn(root, data, tail, old_cat, name, img: gr.File, specs, desc, link, new_cat_input, original_img, is_new):
    if not data: data = {}
    target_cat = new_cat_input if is_new and new_cat_input else old_cat

    if not target_cat: 
        return "没有有效的分类", gr.Dropdown(), data, tail, {}
    
    img_path = ""
    device_img_folder = get_path(root, "public", "images", "device")
    if img:
        img_name, suffix = os.path.split(os.path.basename(img.name))
        dest = os.path.join(device_img_folder, f"{img_name}{suffix}")
        i = 0
        while os.path.exists(dest):
            dest = os.path.join(device_img_folder, f"{img_name}_{i}{suffix}")
            i += 1

        shutil.move(img.name, dest)
        img_path = f"/images/device/{os.path.basename(dest)}"
    elif not is_new:
        img_path = original_img
    
    new_item = {"name": name, "image": img_path, "specs": specs, "description": desc, "link": link}
    
    if target_cat not in data: data[target_cat] = []
    
    if is_new:
        data[target_cat].append(new_item)
    else:
        # 简单更新逻辑
        found = False
        if old_cat in data:
            for i, item in enumerate(data[old_cat]):
                if item['name'] == name:
                    data[old_cat][i] = new_item
                    found = True
                    break
        if not found: data[target_cat].append(new_item)
        
    msg = write_ts_data(root, "devices.ts", "devicesData", data, "devices", tail)
    u = load_devices_ui(root)
    return msg, u[0], u[1], u[2], u[3]

def delete_selected_device(root, data, tail, name, cat):
    print(f"Passing following args to delete_selected_device:\n\
{root} -> root\n\
{data} -> data\n\
{tail} -> tail\n\
{name} -> name\n\
{cat} -> cat\n\
")
    if not name or not cat:
        return "未选中有效的设备"

    found = False
    for item in data[cat]:
        if item["name"] == name:
            data[cat].remove(item)
            found = True
            break
    
    if len(data[cat]) == 0:
        #如果删除之后分类中不再有东西，就把分类也删掉
        #但是如果再删除这个分类后将会导致没有分类，就不删除这个分类
        if len(data) > 1:
            data.pop(cat)

    if not found:
        return "未找到要删除的设备"
    write_ts_data(root, "devices.ts", "devicesData", data, "devices", tail)
    u = load_devices_ui(root)
    return f"删除了名称为{name}的设备", u[0], u[1], u[2], u[3]

# --- Albums ---
def load_albums_ui(root):
    base = get_path(root, "public", "images", "albums")
    if not base or not os.path.exists(base): return [], None
    albums = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    return gr.Dropdown(choices=albums, value=None), None

def select_album(root, album_name):
    if not album_name: 
        return "", "", "", "", "", "masonry", 3, [], gr.Dropdown(choices=[])
    path = get_path(root, "public", "images", "albums", album_name)
    info_path = os.path.join(path, "info.json")
    
    info = {}
    if os.path.exists(info_path):
        try:
            with open(info_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
        except: pass
            
    imgs = []
    img_names = []
    if os.path.exists(path):
        for f in os.listdir(path):
            if f.lower().endswith(('.jpg', '.png', '.jpeg', 'webp', 'gif')):
                imgs.append(os.path.join(path, f))
                img_names.append(f)

    print(f"Returned {img_names} for choosing to delete")
    return (
        info.get('title', ''), info.get('description', ''), info.get('date', ''),
        info.get('location', ''), ",".join(info.get('tags', [])), 
        info.get('layout', 'masonry'), info.get('columns', 3),
        imgs, gr.Dropdown(choices=img_names, value=None)
    )

def create_album_func(root, dir_name, new_dirname, title, desc, date, loc, tags, layout, cols):
    print(f"Passing following args to create_album_func:\n\
{root} -> root\n\
{dir_name} -> dir_name\n\
{new_dirname} -> new_dirname\n\
{title} -> title\n\
{desc} -> desc\n\
{date} -> date\n\
{loc} -> loc\n\
{tags} -> tags\n\
{layout} -> layout\n\
{cols} -> cols\n\
")
    if not title: return "请填入相册标题", gr.Dropdown()
    dn = dir_name if dir_name else (new_dirname if new_dirname else title.replace(" ", "_").lower())
    base = get_path(root, "public", "images", "albums", dn)

    exist_album = os.path.exists(base)
    if (not exist_album and new_dirname) or (exist_album and new_dirname and new_dirname != dir_name):
        print(f"Creating new album with name: {new_dirname}")
        base = get_path(root, "public", "images", "albums", new_dirname)
        if os.path.exists(base):
            u = load_albums_ui(root)
            return "已存在同名相册", u[0]
        else:
            os.makedirs(base)
    elif exist_album and new_dirname and new_dirname == dir_name:
        u = load_albums_ui(root)
        return "已存在同名相册", u[0]
    elif not exist_album and not new_dirname:
        u = load_albums_ui(root)
        return "相册不存在，如果想要创建新相册，请填写新目录名", u[0]
    
    info = {
        "title": title, "description": desc, "date": date, "location": loc,
        "tags": [t.strip() for t in tags.split(',')], "layout": layout, "columns": int(cols)
    }

    with open(os.path.join(base, "info.json"), 'w', encoding='utf-8') as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    u = load_albums_ui(root)
    return f"已保存关于相册{dir_name}的修改", u[0]

def upload_album_image(root, album_name, files, is_cover):
    if len(files) > 1 and is_cover:
        return "只能上传一张封面！"
    if not album_name or not files: return "更新相片失败"
    dest = get_path(root, "public", "images", "albums", album_name)
    for f in files:
        if is_cover:
            basename = os.path.basename(f.name)
            suffix = os.path.splitext(basename)[1]
            covername = f"cover{suffix}"
            dest_path = os.path.join(dest, covername)
            i = 0
            while os.path.exists(dest_path):
                os.rename(dest_path, os.path.join(dest, f"cover_{i}{suffix}"))
                i += 1
            shutil.copy(f.name, dest_path)
            if suffix.lower() != '.jpg':
                convert_to_jpg(dest_path)
        else:
            shutil.copy(f.name, os.path.join(dest, os.path.basename(f.name)))
    return "成功更新相片"

def delete_selected_album(root, album_name):
    print(f"Passing the following args to delete_selected_album:\n\
{root} -> root\n\
{album_name} -> album_name\n\
")
    alb_path = get_path(root, "public", "images", "albums")
    alb_names = os.listdir(alb_path)
    if not album_name or album_name not in alb_names:
        return f"没有找到指定的相册: {album_name}", gr.update(), gr.update(), gr.update()
    else:
        target_full_path = os.path.join(alb_path, album_name)
        try:
            shutil.rmtree(target_full_path)
            u = load_albums_ui(root)
            return f"删除了文件夹名为{album_name}的文件夹", u[0], u[1], []
        except Exception as e:
            print(f"Failed deleting folder: {target_full_path}: {e}")
            return "无法删除指定的文件夹", gr.update(), gr.update(), gr.update()

def delete_selected_img(root, album_name, img_name):
    print(f"Passing the following args to delete_selected_img:\n\
{root} -> root\n\
{album_name} -> album_name\n\
{img_name} -> img_name\n")
    albums_path = get_path(root, "public", "images", "albums")
    if not album_name or not img_name:
        return "没有选中有效的图片", gr.update(), gr.update(), gr.update()
    elif album_name not in os.listdir(albums_path):
        return f"没有选中有效的相册: {album_name}", gr.update(), gr.update(), gr.update()
    else:
        album_path = os.path.join(albums_path, album_name)
        found = False
        img_names = os.listdir(album_path)
        for file_name in img_names:
            if file_name == img_name:
                found = True
                img_path = os.path.join(album_path, img_name)
                try:
                    os.remove(img_path)
                    img_names.remove(img_name)
                    img_paths = [os.path.join(album_path, n) for n in img_names]
                    return f"删除了相册{album_name}中的图片{img_name}", gr.Dropdown(choices=img_names), img_paths, []
                except Exception as e:
                    print(f"Failed to delete image {img_name} for: {e}")
                    return f"未能成功删除图片{img_name}: {e}", gr.update(), gr.update(), gr.update()
        if not found:
            return f"没有找到要删除的图片{img_name}", gr.update(), gr.update(), gr.update()   

def load_selected_img(root, album_name, img_name) -> list:
    print(f"Passing following args to load_selected_img:\n\
{root} -> root\n\
{album_name} -> album_name\n\
{img_name} -> img_name\n\
")

    albums_path = get_path(root, "public", "images", "albums")
    if album_name not in os.listdir(albums_path):
        print(f"Found no matching album: {album_name}")
        return []
    else:
        album_path = os.path.join(albums_path, album_name)
        img_names = os.listdir(album_path)
        if img_name not in img_names:
            print(f"Found no matching image")
            return []
        else:
            img_path = os.path.join(album_path, img_name)
            return [img_path]

# --- Posts ---
def load_posts_ui(root):
    base = get_path(root, "src", "content", "posts")
    if not base or not os.path.exists(base): return []
    return gr.Dropdown(choices=[d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))], value=None)

def select_post(root, post_dir):
    empty = ("", "", "", "", "", "", "", False, False, "", "")
    if not post_dir: return empty
    path = get_path(root, "src", "content", "posts", post_dir, "index.md")
    if not os.path.exists(path): return empty
    try:
        post = frontmatter.load(path)
        m = post.metadata
        return (
            m.get('title', ''), m.get('published', ''), m.get('description', ''),
            ",".join(m.get('tags', [])), m.get('category', ''), m.get('author', ''),
            m.get('permalink', ''), m.get('pinned', False), m.get('draft', False), m.get('image', ''),
            post.content
        )
    except: return empty

def create_save_post(root, post_dir, title, pub, desc, tags, cat, author, perm, pinned, draft, content, md_file, cover, original_img, is_new):
    print(f"Passing following args to create_save_post:\n\
{root} -> root\n\
{post_dir} -> post_dir\n\
{title} -> title\n\
{pub} -> pub\n\
{desc} -> desc\n\
{tags} -> tags\n\
{cat} -> cat\n\
{author} -> author\n\
{perm} -> perm\n\
{pinned} -> pinned\n\
{draft} -> draft\n\
{content} -> content\n\
{md_file} -> md_file\n\
{cover} -> cover\n\
{original_img} -> original_img\n\
{is_new} -> is_new\n\
")
    if is_new:
        dn = perm if perm else title.replace(" ", "_").lower()
        full = get_path(root, "src", "content", "posts", dn)
        if os.path.exists(full): return "Exists", gr.Dropdown()
        os.makedirs(full)
    else:
        full = get_path(root, "src", "content", "posts", post_dir)
        dn = post_dir

    final_text = content
    if md_file:
        with open(md_file.name, 'r', encoding='utf-8') as f:
            raw = f.read()
            try:
                fm = frontmatter.loads(raw)
                final_text = fm.content if fm.metadata else raw
            except: final_text = raw

    img_path = ""
    if cover:
        ext = os.path.splitext(cover.name)[1]
        shutil.move(cover.name, os.path.join(full, f"cover{ext}"))
        if os.path.exists(os.path.join(full, f"cover{ext}")):
            print(f"Moved picture to blog folder")
        convert_to_jpg(os.path.join(full, f"cover{ext}"))
        img_path = f"./cover.jpg"
    elif not is_new and os.path.exists(os.path.join(full, "index.md")):
        img_path = original_img

    pub = date.fromisoformat(pub)
    meta = {
        "title": title, "published": date.fromisoformat(datetime.now().strftime("%Y-%m-%d")),
        "pinned": pinned, "description": desc, "tags": [t.strip() for t in tags.split(',')],
        "category": cat, "author": author, "draft": draft, "date": pub, "pubDate": datetime.now().strftime("%Y-%m-%d"), "image": img_path
    }

    if perm:
        meta["permalink"] = perm
    
    with open(os.path.join(full, "index.md"), 'wb') as f:
        frontmatter.dump(frontmatter.Post(final_text, **meta), f)
    
    return "Saved", load_posts_ui(root)

# --- About ---

def update_about(root, f):
    if not f: return "Fail"
    shutil.copy(f.name, get_path(root, "src", "content", "spec", "about.md"))
    return "Done"

# --- Misc ---

def update_color(color_str: str):
    if color_str.startswith("#"):
        return color_str
    elif color_str.startswith("rgba"):
        color_str = color_str.replace("rgba(", '')
        color_str = color_str.replace(")", '')
        r, g, b, a = [float(x) for x in color_str.strip().split(",")]    
        return colors.to_hex((r/255, g/255, b/255))
    elif color_str.startswith("rgb"):
        color_str.replace("rgb(", "")
        color_str.replace(")", "")
        r, g, b = [int(x) for x in color_str.strip().split(",")]
        color_hex = colors.to_hex((r/255, g/255, b/255))
        return color_hex

# ====================
# UI
# ====================

with gr.Blocks(title="Mizuki CM") as demo:
    gr.Markdown("# Mizuki Blog Content Managemer")
    root_input = gr.Textbox(label="根目录 (绝对路径)", placeholder="D:/xxx")
    
    with gr.Tabs():
        with gr.TabItem("About"):
            f = gr.File(label="about.md", file_types=['.md'])
            b = gr.Button("更新")
            lbl = gr.Label()
            b.click(update_about, [root_input, f], lbl)

        with gr.TabItem("Diary"):
            with gr.Row():
                dr = gr.Button("刷新")
                ds = gr.Dropdown(label="选择日记")
            d_data = gr.State()
            d_tail = gr.State()
            
            with gr.Row():
                with gr.Column():
                    dc = gr.Textbox(label="内容", lines=15)
                with gr.Column():
                    did = gr.Number(label="ID", interactive=False)
                    dm = gr.Textbox(label="心情")
                    dl = gr.Textbox(label="地点")
                    dt = gr.Textbox(label="标签", placeholder="tag1, tag2...")
            dimg = gr.File(label="插入图片", file_count="multiple", file_types=["image"])
            
            with gr.Row():
                d_save = gr.Button("保存")
                d_create = gr.Button("创建")
                d_delete = gr.Button("删除选中的日记", variant="stop")
            d_msg = gr.Label(label="状态")
            
            dr.click(load_diary_ui, [root_input], [ds, d_data, d_tail])
            ds.change(select_diary, [ds, d_data], [did, dc, dm, dl, dt])
            d_save.click(save_diary_entry, [root_input, d_data, d_tail, did, dc, dm, dl, dt, dimg], [d_msg, ds, d_data, d_tail])
            d_create.click(create_diary_entry, [root_input, dc, dm, dl, dt, dimg], [d_msg, ds, d_data, d_tail])
            d_delete.click(delete_selected_diary, inputs=[root_input, d_data, d_tail, did], outputs=[d_msg, ds, d_data, d_tail])

        with gr.TabItem("Friends"):
            fr = gr.Button("刷新")
            fs = gr.Dropdown(label="选择")
            f_data = gr.State()
            f_tail = gr.State()
            
            fid = gr.Number(label="ID", interactive=False)
            ft = gr.Textbox(label="Title")
            fi = gr.Textbox(label="Img")
            fd = gr.Textbox(label="Desc")
            fu = gr.Textbox(label="URL")
            ftags = gr.Textbox(label="Tags")
            
            with gr.Row():
                f_save = gr.Button("保存")
                f_create = gr.Button("创建")
                f_delete = gr.Button("删除选中的友情链接", variant="stop")
            f_msg = gr.Label()
            
            fr.click(load_friends_ui, [root_input], [fs, f_data, f_tail])
            fs.change(select_friend, [fs, f_data], [fid, ft, fi, fd, fu, ftags])
            f_save.click(save_friend_btn, [root_input, f_data, f_tail, fid, ft, fi, fd, fu, ftags], [f_msg, fs, f_data, f_tail])
            f_create.click(create_friend_btn, [root_input, ft, fi, fd, fu, ftags], [f_msg, fs, f_data, f_tail])
            f_delete.click(delete_selected_friend, inputs=[root_input, f_data, f_tail, fid], outputs=[f_msg, fs, f_data, f_tail])

        with gr.TabItem("Projects"):
            pr = gr.Button("刷新")
            ps = gr.Dropdown(label="选择已有项目")
            p_data = gr.State()
            p_tail = gr.State()
            
            pid = gr.Textbox(label="ID")
            pt = gr.Textbox(label="项目标题")
            pcat = gr.Dropdown([("网页应用", "web"), ("移动应用", "mobile"), ("桌面应用", "desktop"), ("其它", "other")], label="项目类别")
            pstat = gr.Dropdown([("已完成", "completed"), ("进行中", "in-progress"), ("已计划", "planned")], label="项目状态")
            pdesc = gr.Textbox(label="项目描述")
            pimg = gr.Textbox(label="项目封面")
            pstack = gr.Textbox(label="技术栈")
            ptags = gr.Textbox(label="标签")
            pdemo = gr.Textbox(label="Demo网址")
            pcode = gr.Textbox(label="源码网址")
            pvisit = gr.Textbox(label="项目主页网址")
            pstart = gr.Textbox(label="开始日期",placeholder="YYYY-MM-DD")
            pend = gr.Textbox(label="结束日期", placeholder="YYYY-MM-DD")
            pfeat = gr.Checkbox(label="是否置顶")
            
            with gr.Row():
                p_save = gr.Button("保存")
                p_create = gr.Button("创建")
                p_delete = gr.Button("删除选中的项目", variant="stop")
            p_msg = gr.Label()
            
            pr.click(load_projects_ui, [root_input], [ps, p_data, p_tail])
            ps.change(select_project, [ps, p_data], [pid, pt, pdesc, pimg, pcat, pstack, pstat, pdemo, pcode, pstart, pend, pfeat, ptags, pvisit])
            p_save.click(fn=save_project_all, inputs=[root_input, p_data, p_tail, pid, pt, pdesc, pimg, pcat, pstack, pstat, pdemo, pcode, pstart, pend, pfeat, ptags, pvisit], outputs=[p_msg, ps, p_data, p_tail])
            p_create.click(fn=save_project_all, inputs=[root_input, p_data, p_tail, pid, pt, pdesc, pimg, pcat, pstack, pstat, pdemo, pcode, pstart, pend, pfeat, ptags, pvisit], outputs=[p_msg, ps, p_data, p_tail])
            p_delete.click(delete_selected_project, inputs=[root_input, p_data, p_tail, pid], outputs=[p_msg, ps, p_data, p_tail])

        with gr.TabItem("Devices"):
            devr = gr.Button("刷新")
            dev_data = gr.State()
            dev_tail = gr.State()
            dev_map = gr.State()
            dev_img = gr.State()
            with gr.Row():
                with gr.Column():
                    dcat = gr.Textbox(label="当前分类", interactive=False)
                    dn = gr.Textbox(label="名称", placeholder="必填")
                    dnewcat = gr.Textbox(label="新分类(如果要创建新的分类, 就填必填，譬如厂商名)")
                    di = gr.File(label="设备图片", file_count="single", file_types=["image"])
                    dsp = gr.Textbox(label="配置与规格", placeholder="必填")
                    dd = gr.Textbox(label="描述", placeholder="必填")
                    dl = gr.Textbox(label="链接")
                with gr.Column():
                    devs = gr.Dropdown(label="选择")
                    dev_save = gr.Button("保存")
                    dev_create = gr.Button("创建")
                    dev_delete = gr.Button("删除选中的设备", variant="stop")
                    dev_msg = gr.Label(label="状态")
            
            devr.click(load_devices_ui, [root_input], [devs, dev_data, dev_tail, dev_map])
            devs.change(select_device, [devs, dev_data, dev_map], [dcat, dn, dev_img, dsp, dd, dl])
            dev_save.click(lambda *a: save_device_btn(*a, False), [root_input, dev_data, dev_tail, dcat, dn, di, dsp, dd, dl, dnewcat, dev_img], [dev_msg, devs, dev_data, dev_tail, dev_map])
            dev_create.click(lambda *a: save_device_btn(*a, True), [root_input, dev_data, dev_tail, dcat, dn, di, dsp, dd, dl, dnewcat, dev_img], [dev_msg, devs, dev_data, dev_tail, dev_map])
            dev_delete.click(delete_selected_device, inputs=[root_input, dev_data, dev_tail, dn, dcat], outputs=[dev_msg, devs, dev_data, dev_tail, dev_map])

        with gr.TabItem("Albums"):
            ar = gr.Button("刷新")
            with gr.Row():
                with gr.Column():
                    anew = gr.Textbox(label="新目录名", placeholder="创建时必填")
                    at = gr.Textbox(label="相册标题", placeholder="必填")
                    adate = gr.Textbox(label="日期", placeholder="YYYY-MM-DD")
                    acol = gr.Number(label="相册列数", value=3)
                    alay = gr.Radio(["masonry", "grid"], label="排版方式")
                    adesc = gr.Textbox(label="描述")
                    aloc = gr.Textbox(label="地点")
                    atags = gr.Textbox(label="标签", placeholder="tag1, tag2...")
                with gr.Column():
                    asl = gr.Dropdown(label="选择相册", allow_custom_value=True)
                    asave = gr.Button("更新信息/创建新相册")
                    adel_alb = gr.Button("删除选中的相册", variant="stop")
                    with gr.Column():
                        aup = gr.File(label="上传图片", file_count="multiple", file_types=["image"])
                        with gr.Row():
                            abtn = gr.Button("上传")
                            acover = gr.Checkbox(label="作为封面上传")
                    amsg = gr.Label(label="状态") 
            with gr.Row():
                with gr.Column():
                    adel_sel = gr.Dropdown(label="选中要删除的图片", allow_custom_value=True, interactive=True)
                    adel_img = gr.Button("删除选中的图片", variant="stop")
                with gr.Column():
                    adel_img_prev = gr.Gallery(label="选中的将要删除的图片")
            agall = gr.Gallery(label="图片总览")
            
            adel_sel.change(fn=load_selected_img, inputs=[root_input, asl, adel_sel], outputs=[adel_img_prev])
            ar.click(load_albums_ui, [root_input], [asl, agall])
            asl.change(select_album, [root_input, asl], [at, adesc, adate, aloc, atags, alay, acol, agall, adel_sel])
            asave.click(fn=create_album_func, inputs=[root_input, asl, anew, at, adesc, adate, aloc, atags, alay, acol], outputs=[amsg, asl])
            adel_img.click(fn=delete_selected_img, inputs=[root_input, asl, adel_sel], outputs=[amsg, adel_sel, agall, adel_img_prev])
            adel_alb.click(fn=delete_selected_album, inputs=[root_input, asl], outputs=[amsg, asl, agall, adel_img_prev])
            abtn.click(upload_album_image, [root_input, asl, aup, acover], [amsg])

        with gr.TabItem("Posts"):
            por = gr.Button("刷新")
            poimg = gr.State()
            with gr.Row():
                with gr.Column():
                    pos = gr.Dropdown(label="选择")
                    pot = gr.Textbox(label="标题")
                    poperm = gr.Textbox(label="快速链接名字", placeholder="选填")
                    pod = gr.Textbox(label="日期", placeholder="YYYY-MM-DD")
                    pocat = gr.Textbox(label="类别")
                    poauth = gr.Textbox(label="作者")
                    podesc = gr.Textbox(label="描述")
                    potags = gr.Textbox(label="标签", placeholder="tag1, tag2, ...")
                    popin = gr.Checkbox(label="是否置顶")
                    podft = gr.Checkbox(label="是否为草稿")
                with gr.Column():
                    pomd = gr.File(label="Markdown文件", file_types=['.md'])
                    pocov = gr.File(label="封面图片", file_types=["image"])
                    posave = gr.Button("保存")
                    pocreate = gr.Button("创建")
                    pomsg = gr.Label(label="状态")
            poct = gr.Textbox(label="内容预览", lines=10)
            
            por.click(load_posts_ui, [root_input], [pos])
            pos.change(select_post, [root_input, pos], [pot, pod, podesc, potags, pocat, poauth, poperm, popin, podft, poimg, poct])
            posave.click(lambda *a: create_save_post(*a, False), [root_input, pos, pot, pod, podesc, potags, pocat, poauth, poperm, popin, podft, poct, pomd, pocov, poimg], [pomsg, pos])
            pocreate.click(lambda *a: create_save_post(*a, True), [root_input, pos, pot, pod, podesc, potags, pocat, poauth, poperm, popin, podft, poct, pomd, pocov, poimg], [pomsg, pos])

        with gr.TabItem("Timeline"):
            tir = gr.Button("刷新")
            ti_data = gr.State()
            ti_tail = gr.State()
            with gr.Row():
                with gr.Column():
                    tiid = gr.Textbox(label="ID", placeholder="创建时必填")
                    tititle = gr.Textbox(label="事件标题", placeholder="必填")
                    tides = gr.Textbox(label="描述", lines=5, placeholder="必填")
                    titype = gr.Dropdown(label="事件类型", choices=["教育", "证书", "项目", "其它"])
                    tidate = gr.Textbox(label="日期", placeholder="YYYY-MM-DD")
                    tiloc = gr.Textbox(label="地点")
                    tiorg = gr.Textbox(label="组织或机构")
                    tiski = gr.Textbox(label="相关技能", placeholder="skill1,skill2, ...")
                    tifeat = gr.Checkbox(label="是否置顶")

                with gr.Column():
                    tis = gr.Dropdown(label="选择时间线事件", allow_custom_value=True)
                    tisave = gr.Button("保存/创建")
                    tidelete = gr.Button("删除选中的时间线事件", variant="stop")
                    timsg = gr.Label(label="状态")

            tir.click(load_timeline_ui, [root_input], [tis, ti_data, ti_tail])
            tis.change(select_timeline, [tis, ti_data], [tiid, tititle, tides, titype, tidate, tiloc, tiorg, tiski, tifeat])
            tisave.click(save_timeline, [root_input, ti_data, ti_tail, tiid, tititle, tides, titype, tidate, tiloc, tiorg, tiski, tifeat], [timsg, tis, ti_data, ti_tail])
            tidelete.click(delete_selected_timeline, [root_input, ti_data, ti_tail, tiid], [timsg, tis, ti_data, ti_tail])
        
        with gr.TabItem("Skills"):
            sr = gr.Button("刷新")
            s_data = gr.State()
            s_tail = gr.State()
            
            with gr.Row():
                with gr.Column():
                    sd = gr.Number(label="ID", placeholder="创建时必填")
                    sname = gr.Textbox(label="技能名称", placeholder="必填")
                    sdesc = gr.Textbox(label="描述", lines=5, placeholder="必填")
                    scat = gr.Dropdown(label="分类", choices=[("前端", "frontend"), ("后端", "backend"), ("数据库", "database"), ("软件", "tools"), ("其它", "other")])
                    slevel = gr.Dropdown(label="熟练度", choices=[("入门", "beginner"), ("中等", "intermediate"), ("高级", "advanced"), ("专家", "expert")])
                    sexp_yr = gr.Number(label="经验长度（年）", value=0, step=1, precision=0, minimum=0, maximum=100,interactive=True)
                    sexp_mo = gr.Number(label="经验长度（月）", value=0, step=1, precision=0, minimum=0, maximum=11, interactive=True)
                    sico = gr.Textbox(label="图标", placeholder="选填，使用iconify图标库用于astro的图标名称，譬如material-symbols:school")
                with gr.Column():
                    ss = gr.Dropdown(label="选择技能", allow_custom_value=True)
                    with gr.Row():
                        scolor_input = gr.ColorPicker(label="(可选)自定义技能条目的颜色")
                        scolor_hex = gr.Textbox(label="颜色的Hex值", interactive=False)
                    screate = gr.Button("保存/创建")
                    sdelete = gr.Button("删除选中的技能", variant="stop")
                    simsg = gr.Label(label="状态")

            sr.click(load_skills_ui, [root_input], [ss, s_data, s_tail])
            ss.change(select_skill, [ss, s_data], [sd, sname, sdesc, sico, scat, slevel, sexp_yr, sexp_mo, scolor_hex])
            scolor_input.change(update_color, scolor_input, [scolor_hex])
            screate.click(save_skill, [root_input, s_data, s_tail, sd, sname, sdesc, sico, scat, slevel, sexp_yr, sexp_mo, scolor_hex], [simsg, ss, s_data, s_tail])
            sdelete.click(delete_selected_skill, [root_input, s_data, s_tail, sd], [simsg, ss, s_data, s_tail])


# 授权所有驱动器，防止 InvalidPathError
allowed = get_allowed_paths()
allowed.append(os.getcwd())

if __name__ == "__main__":
    demo.launch(allowed_paths=allowed)