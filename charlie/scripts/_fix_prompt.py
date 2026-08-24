import os

fp = "/Users/sxliuyu/orca/projects/charlie/charlie/agent/system_msg.py"
with open(fp, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Debug
print("Line 149:", repr(lines[148]))
print("Line 153:", repr(lines[152]))
print("Line 155:", repr(lines[154]))

# Replace line 149 (0-indexed 148): Rule 2
lines[148] = '        "2. 简洁。能1句说完不用2句。但如果用户问的是开放性问题，可以多说几句。\\n"
'

# Replace line 153 (0-indexed 152): Rule 6
lines[152] = '        "6. ASR碎片或听不懂时回\"没听清，再说一遍？\"，不要猜用户意思。\\n"
'

# Replace line 155 (0-indexed 154): Rule 8
lines[154] = '        "8. 用户说\"对\"\"好的\"\"嗯\"等确认词时，结合上下文理解——这些词往往指刚聊的话题，不要当作新对话。\\n"
'

# Insert rules 9 and 10
lines.insert(155, '        "9. 多轮对话时，记住前面聊过的内容。用户说\"那个\"\"它\"\"这个\"时，指代上文提到的事物。\\n"
')
lines.insert(156, '        "10. 用户说的话有语法不通或残缺时(语音识别误差)，结合上下文推断真实意图，尽量回应而非说\"听不懂\"。\\n"
')

with open(fp, "w", encoding="utf-8") as f:
    f.writelines(lines)

print("done")
