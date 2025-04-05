import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import *
import pygsheets
import re
from datetime import datetime
import pytz
import threading
import google.generativeai as genai
import time
import numpy as np
# from sentence_transformers import SentenceTransformer
from rank_bm25 import BM25Okapi
import jieba

# 設定 GMT+8 時區
gmt_8 = pytz.timezone("Asia/Taipei")
timestamp = datetime.now(gmt_8).strftime("%Y-%m-%d %H:%M:%S")

app = Flask(__name__)

# 設定版本代碼
version_code = "25.04.05.2222"
print(f"Starting application - Version Code: {version_code}")

gemini_api_key = os.getenv('GEMINI_API_KEY')
# 初始化 Gemini API
genai.configure(api_key=gemini_api_key)
generation_model = genai.GenerativeModel('gemini-2.0-flash')

# 設定LINE機器人和Google Sheets API
line_bot_api = LineBotApi(os.environ.get('LINE_BOT_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_BOT_CHANNEL_SECRET'))
gc = pygsheets.authorize(service_account_file='service_account_key.json')
sheet = gc.open_by_url('https://docs.google.com/spreadsheets/d/1WSgGzCDKBlzKPPIAqQhKOn2GK_xQ6Y2TZHjEiWDrOVM/')


#取得主要問題
main_ws = sheet.worksheet('title', '表單回應 1')
# 取得 "欄位A" 和 "欄位C" 的值
main_questions_in_sheet = main_ws.get_col(3, include_tailing_empty=False)
main_answers_in_sheet = main_ws.get_col(5, include_tailing_empty=False)

cpc_ws = sheet.worksheet('title', '中油點數')
# 取得 "CPC問題" 和 "CPC點數" 的值
cpc_questions_in_sheet = cpc_ws.get_col(8, include_tailing_empty=False)
cpc_answers_in_sheet = cpc_ws.get_col(9, include_tailing_empty=False)
cpc_list = cpc_ws.get_col(1, include_tailing_empty=False)

questions_in_sheet = main_questions_in_sheet + cpc_questions_in_sheet
answers_in_sheet = main_answers_in_sheet + cpc_answers_in_sheet

#TODO:資料向量化(改到googlescript)
# 先對問句進行分詞
tokenized_questions = [list(jieba.cut(q)) for q in questions_in_sheet]

# 建立 BM25 模型
bm25 = BM25Okapi(tokenized_questions)

# 載入中文句向量模型
_model = None

def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
    return _model

# 將問句轉換為向量
question_embeddings = get_model().encode(questions_in_sheet)

#TODO:建立同義詞字典(放到google sheet讀取)
synonym_list = [
    ["無法連線", "離線", "網路斷線", "連不上"],
]
synonym_dict = {}
for synonyms in synonym_list:
    for word in synonyms:
        synonym_dict[word] = set(synonyms) - {word}  # 除了自己以外的詞都是同義詞

def expand_query(query):
    words = jieba.lcut(query)
    expanded_words = set(words)  # 原始詞

    for word in words:
        if word in synonym_dict:
            expanded_words.update(synonym_dict[word])  # 加入同義詞

    return " ".join(expanded_words)  # 轉回字串

def retrieve_top_n(query, n=2, threshold=5, high_threshold=10):
    """取得最相似的問題

    ##作法
    1.使用Sentence Transformers進行相似度計算
    2.使用BM25強化搜索
    3.閥值為5，超過才列為答案
    4.最多選擇2個答案 
    """
    try:

      #替換查詢詞中的同義詞
      expanded_query = expand_query(query)
      # BM25 排序
      tokenized_query = list(jieba.cut(query))
      bm25_scores = bm25.get_scores(tokenized_query)

      # Sentence Transformers 相似度計算
      query_embedding = get_model().encode([query])[0]
      semantic_scores = np.dot(question_embeddings, query_embedding)  # 餘弦相似度

      # 兩者加權平均（可調整權重）
      combined_scores = 0.7 * np.array(bm25_scores) + 0.3 * semantic_scores

      # 1. 篩選出超過基本閾值的結果
      above_threshold_indices = [i for i, score in enumerate(combined_scores) if score >= threshold]
      
      # 如果沒有結果超過閾值，返回空列表
      if not above_threshold_indices:
          return []
      
      # 按分數排序
      sorted_indices = sorted(above_threshold_indices, key=lambda i: combined_scores[i], reverse=True)

      # 2. 檢查有多少結果超過高閾值
      high_score_indices = [i for i in sorted_indices if combined_scores[i] >= high_threshold]
      
      # 3. 根據高分結果數量決定返回多少結果
      esult = []
      if len(high_score_indices) >= 2:
          # 如果有兩個或以上高分結果，返回前2個
          result = [(questions_in_sheet[i], answers_in_sheet[i]) for i in high_score_indices[:2]]
          # 在新執行緒中記錄問題
          thread = threading.Thread(target=record_question_for_answer, args=(questions_in_sheet[high_score_indices[0]],))
          thread.start()
      else:
          # 如果沒有或只有一個高分結果，只返回最高分的一個
          result = [(questions_in_sheet[sorted_indices[0]], answers_in_sheet[sorted_indices[0]])]
          # 在新執行緒中記錄問題
          thread = threading.Thread(target=record_question_for_answer, args=(questions_in_sheet[sorted_indices[0]],))
          thread.start()
      
      return result
    except Exception as e:
      print(f"Error in retrieve_top_n: {str(e)}")
      return [] 

def reply_by_LLM(finalanswer,model):
  try:

    prompt = f"""請將{ finalanswer }直接轉成自然語言。
    ##條件
    1.口氣禮貌親切簡潔
    2.若finalanswer為空[]，則回覆:此問題目前找不到合適解答，請聯絡積慧幫忙協助
    3.不要解釋以上回覆條件，直接回覆答案
    """
    answer_in_human = model.generate_content(prompt)
    return answer_in_human
  except Exception as e:
      print(f"Error in reply_by_AI: {str(e)}")
      return None

def extract_chinese_results_new(response):
    """從模型的回應中提取並解碼中文。

    Args:
        response: 模型產生的 GenerateContentResponse 物件。

    Returns:
        解碼後的中文文本內容，或空字串。
    """
    try:
        text_content = response.candidates[0].content.parts[0].text

        if '\\u' in text_content:
            decoded_text = text_content.encode().decode('unicode_escape')
            return decoded_text

        return text_content
    except (AttributeError, IndexError, UnicodeError):
        return ""

#找出最近似問題並用LLM回答
def find_closest_question_and_llm_reply(query):
  try:
    top_matches = retrieve_top_n(query)
    result = reply_by_LLM(top_matches,generation_model)
    answer_to_line = extract_chinese_results_new(result)
    return answer_to_line
  except Exception as e:
      print(f"Error in find_closest_question_and_llm_reply: {str(e)}")
      return "此問題目前找不到合適解答，請聯絡積慧幫忙協助"


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

# 生成分頁顯示的Flex Message，第一頁分類
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

    return FlexSendMessage(
        alt_text="請選擇問題分類",
        contents=CarouselContainer(contents=[category_bubble])
    )

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

# 紀錄用戶問題到 '回答' 工作表，包含時間和問題
def record_question_for_answer(question_for_answer):   
    try:
        reply_ws = sheet.worksheet('title', '回答')
        print("Found '回答' worksheet.")
    except pygsheets.WorksheetNotFound:
        reply_ws = sheet.add_worksheet('回答')
        reply_ws.update_row(1, ["時間","問題"])
        print("Created '回答' worksheet.")
    timestamp = datetime.now(gmt_8).strftime("%Y-%m-%d %H:%M:%S")
    record_data = [timestamp, question_for_answer]
    reply_ws.insert_rows(row=1, values=record_data, inherit=True)
    print(f"Recorded question: {record_data}")

def get_oil_points_column_a():

    # 如果 A 欄沒有內容
    if not cpc_list or len(cpc_list) == 0:
        return "中油點數表單的 A 欄沒有資料。"

    # 合併所有資料成文字訊息
    return "\n".join(cpc_list)

# 處理來自 LINE 的消息
@app.route("/callback", methods=['POST'])
def callback(request):
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

    if user_input.startswith("知識寶典"):
        # 出現問題選單
        reply = create_category_and_common_features()
        print("Displayed category and common features message.")
        
    elif user_input.startswith("問題分類:"):
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
        solution = find_solution_by_click_question(question)
        
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

    elif user_input == "查詢中油點數":
        oil_points_message = get_oil_points_column_a()
        reply = TextSendMessage(text=oil_points_message)
        print("Displayed '中油兌換點數' column A.")

    else:
      try:
        results = find_closest_question_and_llm_reply(user_input)
        reply = TextSendMessage(text=results)
        print(f"Show LLM answer for question: {user_input}")
      except Exception as e:
        print(f"Error in find_closest_question_and_llm_reply: {str(e)}")
        reply = TextSendMessage(text="機器人暫時無法使用，請聯絡積慧幫忙協助")


    try:
        line_bot_api.reply_message(event.reply_token, reply)
        print("Reply sent successfully.")
    except LineBotApiError as e:
        print(f"Failed to send reply: {e}")

    # 非同步地記錄用戶的提問
    thread = threading.Thread(target=record_question, args=(user_id, user_input))
    # 預設 daemon=False
    thread.start()


#找對應到的解決方式
def find_solution_by_click_question(question_text):
    try:
        # 獲取完整的工作表數據
        main_ws = sheet.worksheet('title', '表單回應 1')
        all_data = main_ws.get_all_values()

        # 假設第一行是標題行
        # C 欄是索引 2，E 欄是索引 4
        for row in all_data[1:]:  # 跳過標題行
            if len(row) > 4 and row[2].strip() == question_text.strip():  # 檢查 C 欄
                solution = row[4].strip()  # 獲取 E 欄內容
                print(f"Found solution for question '{question_text}': {solution}")
                return solution

        print(f"No solution found for question '{question_text}'")
        return None
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