from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import *
import os
import pygsheets
import re
from datetime import datetime
import pytz
import threading
import google.generativeai as genai

# 設定 GMT+8 時區
gmt_8 = pytz.timezone("Asia/Taipei")
timestamp = datetime.now(gmt_8).strftime("%Y-%m-%d %H:%M:%S")

app = Flask(__name__)

# 設定版本代碼
version_code = "24.11.02.2222"
print(f"Starting application - Version Code: {version_code}")

gemini_api_key = os.getenv('GEMINI_API_KEY')
# 初始化 Gemini API
genai.configure(api_key=gemini_api_key)

# 設定LINE機器人和Google Sheets API
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))
#CC
# gc = pygsheets.authorize(service_account_file='./credentials.json')
# sheet = gc.open_by_url('https://docs.google.com/spreadsheets/d/1U-C4LvkMKuybwckanHkD8FJoEB0f4D0dPxu8D54jEMI/')

# 獲取 "熱門排行" 工作表中的前5個問題，並從主工作表獲取完整的問題描述
def get_top_questions():
    try:
        # 獲取 "熱門排行" 工作表
        ranking_ws = sheet.worksheet('title', '熱門排行')
        print("Found '熱門排行' worksheet.")
    except pygsheets.WorksheetNotFound:
        print("熱門排行 worksheet not found.")
        return []

    # 獲取前5個排名和項目
    top_ranking_records = ranking_ws.get_all_records()[:5]
    top_questions = []

    # 獲取主工作表以查找完整問題描述
    main_ws = sheet.worksheet('title', '表單回應 1')
    main_records = main_ws.get_all_records()

    for record in top_ranking_records:
        # 根據 "項目" 查找完整的問題描述
        full_question = next((item for item in main_records if item['問題描述'] == record['項目']), None)
        if full_question:
            top_questions.append({
                "排名": record['排名'],
                "項目": record['項目'],
                "問題描述": full_question['問題描述'],
                "解決方式": full_question['解決方式']
            })

    print(f"Top 5 questions with descriptions: {top_questions}")
    return top_questions

# 紀錄用戶問題到 '統計紀錄' 工作表，包含用戶名稱
def record_question(user_id, user_input):
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
        print(f"Fetched user profile: {user_name}")
    except LineBotApiError as e:
        user_name = "Unknown"
        print(f"Error getting user profile: {e}")

    try:
        stats_ws = sheet.worksheet('title', '統計紀錄')
        print("Found '統計紀錄' worksheet.")
    except pygsheets.WorksheetNotFound:
        stats_ws = sheet.add_worksheet('統計紀錄')
        stats_ws.update_row(1, ["時間", "使用者ID", "使用者名稱", "詢問文字"])
        print("Created '統計紀錄' worksheet.")

    timestamp = datetime.now(gmt_8).strftime("%Y-%m-%d %H:%M:%S")
    record_data = [timestamp, user_id, user_name, user_input]
    stats_ws.insert_rows(row=1, values=record_data, inherit=True)
    print(f"Recorded question: {record_data}")

# 生成分頁顯示的Flex Message，包含第一頁分類和第二頁常用功能
def create_category_and_common_features():
    print("Generating category and common features message.")
    categories = get_unique_categories()
    category_bubble = BubbleContainer(
        body=BoxComponent(
            layout="vertical",
            contents=[
                TextComponent(text="請選擇問題分類", weight="bold", size="xl", margin="md")
            ] + [
                TextComponent(
                    text=f"{idx + 1}. {category}",
                    size="md",
                    color="#4682B4",
                    wrap=True,
                    margin="md",
                    action=MessageAction(label=category, text=f"問題分類: {category}")
                ) for idx, category in enumerate(categories[:10])
            ]
        )
    )

    common_features_bubble = BubbleContainer(
        body=BoxComponent(
            layout="vertical",
            contents=[
                TextComponent(text="常用功能", weight="bold", size="xl", margin="md"),
                TextComponent(
                    text="1. 油品通訊錄查詢",
                    size="md",
                    color="#4682B4",
                    wrap=True,
                    margin="md",
                    action=URIAction(
                        label="油品通訊錄查詢",
                        uri="https://docs.google.com/spreadsheets/d/1fPV2FmlC6SPs8n__6l8M6ZzfWQR-uFsR/edit?usp=sharing&ouid=102425893651001429385&rtpof=true&sd=true"
                    )
                ),
                TextComponent(
                    text="2. 中油點數查詢",
                    size="md",
                    color="#4682B4",
                    wrap=True,
                    margin="md",
                    action=URIAction(
                        label="中油點數查詢",
                        uri="https://docs.google.com/spreadsheets/d/1Zxh81gHSr-qIRmMZqHVEG5h1NYhZAWfGp2c23izHMF4/edit?usp=sharing"
                    )
                ),
                TextComponent(
                    text="3. 促銷活動",
                    size="md",
                    color="#4682B4",
                    wrap=True,
                    margin="md",
                    action=URIAction(
                        label="促銷活動",
                        uri="https://sites.google.com/view/taisugar/%E9%A6%96%E9%A0%81"
                    )
                )
            ]
        )
    )

    return FlexSendMessage(
        alt_text="請選擇分類或常用功能",
        contents=CarouselContainer(contents=[category_bubble, common_features_bubble])
    )

def get_oil_points_column_a():
    try:
        points_ws = sheet.worksheet('title', '中油點數')
        print("Found '中油點數' worksheet.")
    except pygsheets.WorksheetNotFound:
        print("中油點數 worksheet not found.")
        return "未找到 '中油點數' 工作表。"

    # 獲取 A 欄的所有內容
    column_a = points_ws.get_col(1, include_tailing_empty=False)
    print(f"Column A from '中油點數': {column_a}")

    # 如果 A 欄沒有內容
    if not column_a or len(column_a) == 0:
        return "中油點數表單的 A 欄沒有資料。"

    # 合併所有資料成文字訊息
    return "\n".join(column_a)

# 處理來自 LINE 的消息
@app.route("/callback", methods=['POST'])
def callback():
    print(f"Version Code: {version_code}")
    
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    print("Request body:", body)

    try:
        handler.handle(body, signature)
        print("Message handled successfully.")
    except InvalidSignatureError as e:
        print("InvalidSignatureError:", e)
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_input = event.message.text
    user_id = event.source.user_id

    if user_input.startswith("問題分類:"):
        # 提取分類名稱並移除多餘空格
        category = user_input.replace("問題分類:", "", 1).strip()
        print(f"Processing category request: '{category}'")
        
        # 根據分類獲取問題
        questions = get_questions_by_category(category)
        
        if questions:
            print(f"Found {len(questions)} questions for category '{category}'")
            reply = create_flex_message(f"{category} - 問題列表", questions, "question")
        else:
            print(f"No questions found for category '{category}'")
            reply = TextSendMessage(text=f"找不到「{category}」分類的相關問題。請確認分類名稱是否正確。")

    elif user_input.startswith("問題:"):
        # 提取問題描述
        question = user_input.replace("問題:", "", 1).strip()
        print(f"Looking for solution to question: '{question}'")
        

        # 使用新的函數查找 E 欄的解決方式
        solution = find_solution_by_question(question)
        
        if solution:
            # 只顯示解決方式，不再顯示問題描述
            reply_contents = [
                TextComponent(text="解決方式", weight="bold", size="lg", margin="md"),
                TextComponent(text=solution, size="sm", color="#6A5ACD", wrap=True, margin="md"),
                SeparatorComponent(margin="md"),
                TextComponent(
                    text="🔙 返回問題分類",
                    weight="bold",
                    color="#228B22",
                    wrap=True,
                    margin="md",
                    action=MessageAction(
                        label="返回問題分類",
                        text="請選擇問題分類"
                    )
                )
            ]
            
            reply = FlexSendMessage(
                alt_text="解決方式",
                contents=BubbleContainer(
                    body=BoxComponent(
                        layout="vertical",
                        contents=reply_contents,
                        padding_all="xl"
                    )
                )
            )
            print(f"Displayed solution for question: {question}")
        else:
            reply = TextSendMessage(text="找不到該問題的解決方式。")
            print(f"No solution found for question: {question}")

    elif user_input == "熱門詢問":
        top_questions = get_top_questions()
        if top_questions:
            reply = create_flex_message("熱門詢問 - Top 5 問題", top_questions, "question")
        else:
            reply = TextSendMessage(text="目前沒有熱門排行記錄。")
        print("Displayed top 5 questions.")

    elif user_input == "中油兌換點數":
        oil_points_message = get_oil_points_column_a()
        reply = TextSendMessage(text=oil_points_message)
        print("Displayed '中油兌換點數' column A.")

    else:
        results = search_by_keyword(user_input)
        if results:
            reply = create_flex_message("搜尋結果", results, "question")
            print("Search results found.")
        else:
            reply = create_category_and_common_features()
            print("No results found, displaying category options.")

    # 統一的快速回覆按鈕
    quick_reply = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="熱門詢問", text="熱門詢問")),
        QuickReplyButton(action=MessageAction(label="中油兌換點數", text="中油兌換點數"))
    ])

    if isinstance(reply, FlexSendMessage):
        reply.quick_reply = quick_reply
    else:
        reply = TextSendMessage(text=reply.text, quick_reply=quick_reply)

    try:
        line_bot_api.reply_message(event.reply_token, reply)
        print("Reply sent successfully.")
    except LineBotApiError as e:
        print(f"Failed to send reply: {e}")
    
    # 创建并启动后台线程
    thread = threading.Thread(target=record_question, args=(user_id, user_input))
    # 默认 daemon=False，不需要显式设置
    thread.start()

#找對應到的解決方式
def find_solution_by_question(question_text):
    try:
        # 獲取完整的工作表數據
        main_ws = sheet.worksheet('title', '表單回應 1')
        all_data = main_ws.get_all_values()
        
        # 假設第一行是標題行
        # C 欄是索引 2，E 欄是索引 4

        #CC
        #  1. 使用 LLM 搜尋相關問題
        model = genai.GenerativeModel('gemini-2.0-flash')
        best_match = None
        best_match_score = 0
        for row in all_data[1:]:  # 跳過標題行
            if len(row) > 4:
                question_in_sheets = row[2].strip()  # C 欄的問題描述

                # 使用 LLM 計算問題的相似度
                prompt = f"請判斷以下兩個問題的相似度，並返回一個 0 到 1 之間的數字，1 代表完全相同，0 代表完全不同：\n問題1：{question_text}\n問題2：{question_in_sheets}\n相似度："
                response = model.generate_content(prompt)

                try:
                    similarity_score = float(response.text)
                except ValueError:
                    similarity_score = 0

                # 更新最佳匹配
                if similarity_score > best_match_score:
                    best_match_score = similarity_score
                    best_match = row

        # 2. 如果找到相似度足夠高的問題，則返回答案
        if best_match and best_match_score > 0.7:  # 設定相似度閾值
            solution = best_match[4].strip()  # 獲取 E 欄內容 (解決方式)
            print(f"在 Google Sheets 中找到相似問題，相似度：{best_match_score}，答案：{solution}")
            return solution
        else:
            print(f"在 Google Sheets 中找不到相似問題，最佳相似度：{best_match_score}")
            return None
        # for row in all_data[1:]:  # 跳過標題行
        #     if len(row) > 4 and row[2].strip() == question_text.strip():  # 檢查 C 欄
        #         solution = row[4].strip()  # 獲取 E 欄內容
        #         print(f"Found solution for question '{question_text}': {solution}")
        #         return solution
        
        # print(f"No solution found for question '{question_text}'")
        # return None
    except Exception as e:
        print(f"Error in find_solution_by_question: {str(e)}")
        return None


# 獲取唯一問題分類
def get_unique_categories():
    try:
        main_ws = sheet.worksheet('title', '表單回應 1')
        # 直接獲取 B 欄（問題分類）的所有值
        categories_column = main_ws.get_col(2)  # 2 代表 B 欄
        # 移除標題行，過濾空值，並取得唯一值
        unique_categories = sorted(list(set(cat.strip() for cat in categories_column[1:] if cat.strip())))
        
        print(f"Found {len(unique_categories)} unique categories from column B: {unique_categories}")
        return unique_categories
    except Exception as e:
        print(f"Error in get_unique_categories: {str(e)}")
        return []

def get_questions_by_category(category):
    try:
        # 獲取完整的工作表數據
        main_ws = sheet.worksheet('title', '表單回應 1')
        all_data = main_ws.get_all_values()
        
        # 假設第一行是標題行
        # B 欄是索引 1，C 欄是索引 2
        questions = []
        
        # 從第二行開始遍歷（跳過標題行）
        for row in all_data[1:]:
            if len(row) > 2 and row[1].strip() == category.strip():  # 檢查 B 欄
                question_text = row[2].strip()  # 獲取 C 欄內容
                if question_text:  # 確保內容不為空
                    questions.append({
                        "問題描述": question_text,
                        "解決方式": ""  # 如果需要其他欄位的內容，可以在這裡添加
                    })
                    print(f"Found matching question for category '{category}': {question_text}")
        
        print(f"Total {len(questions)} questions found for category '{category}'")
        return questions
    except Exception as e:
        print(f"Error in get_questions_by_category: {str(e)}")
        return []


# 搜尋問題描述或解決方案
def search_by_keyword(keywords):
    # 將輸入的關鍵字轉換為小寫並分隔成列表
    keywords_list = [kw.lower().strip() for kw in keywords.split()]
    
    # 定義結果列表
    combined_results = []

    # 搜尋主工作表
    main_ws = sheet.worksheet('title', '表單回應 1')
    records_main = main_ws.get_all_records()
    results_main = [
        {
            '問題描述': str(row['問題描述']),
            '解決方式': str(row['解決方式'])
        }
        for row in records_main
        if '問題描述' in row and '解決方式' in row
        and any(keyword in str(row['問題描述']).lower() or keyword in str(row['解決方式']).lower() for keyword in keywords_list)
    ]
    print(f"Search results from main worksheet for '{keywords}': {results_main}")
    combined_results.extend(results_main)
    
    # 搜尋 "中油點數" 工作表
    try:
        points_ws = sheet.worksheet('title', '中油點數')
        print("Found '中油點數' worksheet.")
        records_points = points_ws.get_all_records()
        results_points = [
            {
                '問題描述': str(row['問題描述']),
                '解決方式': str(row['解決方式'])
            }
            for row in records_points
            if '問題描述' in row and '解決方式' in row
            and any(keyword in str(row['問題描述']).lower() or keyword in str(row['解決方式']).lower() for keyword in keywords_list)
        ]
        print(f"Search results from '中油點數' worksheet for '{keywords}': {results_points}")
        combined_results.extend(results_points)
    except pygsheets.WorksheetNotFound:
        print("中油點數 worksheet not found.")

    # 移除重複的結果（如果兩個工作表有重複的問題）
    unique_results = {frozenset(item.items()): item for item in combined_results}.values()
    combined_results = list(unique_results)

    print(f"Combined and unique search results for '{keywords}': {combined_results}")
    return combined_results





# 回覆結尾附加返回選項，並在上方留空一行，改為綠色
def append_return_option(reply_contents):
    reply_contents.append(TextComponent(text=" ", margin="md"))  # 空行
    reply_contents.append(SeparatorComponent(margin="md"))
    reply_contents.append(TextComponent(
        text="🔙 問題分類", weight="bold", color="#228B22", wrap=True,
        action=MessageAction(label="問題分類", text="請選擇問題分類")
    ))
    print("Return option appended.")
    return FlexSendMessage(
        alt_text="問題解決方式",
        contents=BubbleContainer(body=BoxComponent(layout="vertical", contents=reply_contents))
    )

# 生成Flex Message以顯示搜尋結果或分類選項
def create_flex_message(title, items, item_type="category", start_index=1):
    bubbles = []
    for i in range(0, len(items), 10):  # 每頁顯示10項
        bubble_contents = [TextComponent(text=title, weight="bold", size="xl", margin="md")]

        for idx, item in enumerate(items[i:i+10], start=start_index):
            label_text = f"{idx}. {item['問題描述'] if item_type == 'question' else item}"
            action_text = f"問題: {item['問題描述']}" if item_type == "question" else f"問題分類: {item}"
            
            bubble_contents.append(TextComponent(
                text=label_text, size="md", color="#4682B4", wrap=True, margin="md",
                action=MessageAction(label=label_text[:20], text=action_text)
            ))

        bubble_contents.append(SeparatorComponent(margin="md"))
        bubble_contents.append(TextComponent(
            text="🔙 問題分類", weight="bold", color="#228B22", wrap=True,
            action=MessageAction(label="問題分類", text="請選擇問題分類")
        ))

        bubbles.append(BubbleContainer(body=BoxComponent(layout="vertical", contents=bubble_contents)))
        start_index += 10

    print(f"Generated Flex Message with title '{title}' and {len(bubbles)} bubbles.")
    return FlexSendMessage(alt_text="請選擇分類或問題描述", contents=CarouselContainer(contents=bubbles)) if bubbles else TextSendMessage(text="找不到符合條件的資料。")

# 運行應用
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    print(f"Running on port {port}")
    app.run(host='0.0.0.0', port=port)