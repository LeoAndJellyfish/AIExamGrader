import os
import streamlit as st
import json
from transformers import AutoModel, AutoTokenizer, AutoModelForCausalLM
import torch
from typing import List
import numpy as np
import pickle

# 设置标题和描述
st.title("💬 Yuan2.0 AIReader")
st.write("一个结合了 RAG（检索增强生成）的名著阅读助手。")

# 加载名著清单 JSON 文件
def load_books():
    try:
        with open('./code/books.json', 'r', encoding='utf-8') as f:
            books_data = json.load(f)
        return books_data["books"]
    except Exception as e:
        st.error(f"加载名著清单时出错: {e}")
        return []

books = load_books()

# 名著选择框
book_names = [book["name"] for book in books]
book_selection = st.selectbox("请选择你想提问的名著：", book_names)

# 根据选择的名著获取对应的 document_path
def get_document_path(selected_book_name):
    for book in books:
        if book["name"] == selected_book_name:
            return book["document_path"]
    st.error("未找到对应的文档路径")
    return None

document_path = get_document_path(book_selection)

# 定义一个函数，用于加载模型和tokenizer
@st.cache_resource
def load_model_and_tokenizer():
    try:
        st.session_state.loading_message = st.empty()
        st.session_state.loading_message.write("正在加载模型，请稍候...")
        
        tokenizer = AutoTokenizer.from_pretrained(model_path, add_eos_token=False, add_bos_token=False, eos_token='<eod>')
        tokenizer.add_tokens(['<sep>', '<pad>', '<mask>', '<predict>', '<FIM_SUFFIX>', '<FIM_PREFIX>', '<FIM_MIDDLE>',
                              '<commit_before>', '<commit_msg>', '<commit_after>', '<jupyter_start>', '<jupyter_text>',
                              '<jupyter_code>', '<jupyter_output>', '<empty_output>'], special_tokens=True)

        model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch_dtype, trust_remote_code=True).cuda()

        st.session_state.tokenizer = tokenizer
        st.session_state.model = model

        # 清除模型加载提示
        st.session_state.loading_message.empty()
        st.session_state.loading_message = None
        
        st.session_state.model_loaded = True
        st.write("模型加载完成。")
        return tokenizer, model
    except Exception as e:
        st.error(f"加载模型时出错: {e}")
        return None, None

# 加载模型和tokenizer
model_path = './IEITYuan/Yuan2-2B-Mars-hf'
torch_dtype = torch.bfloat16  # A10
if 'model_loaded' not in st.session_state:
    tokenizer, model = load_model_and_tokenizer()
else:
    tokenizer = st.session_state.tokenizer
    model = st.session_state.model

# 定义向量模型类
class EmbeddingModel:
    def __init__(self, path: str) -> None:
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(path)
            self.model = AutoModel.from_pretrained(path).cuda()
        except Exception as e:
            st.error(f"加载Embedding模型时出错: {e}")
            raise

    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        try:
            encoded_input = self.tokenizer(texts, padding=True, truncation=True, return_tensors='pt')
            encoded_input = {k: v.cuda() for k, v in encoded_input.items()}
            with torch.no_grad():
                model_output = self.model(**encoded_input)
                sentence_embeddings = model_output[0][:, 0]
            sentence_embeddings = torch.nn.functional.normalize(sentence_embeddings, p=2, dim=1)
            return sentence_embeddings.cpu().tolist()
        except Exception as e:
            st.error(f"获取嵌入时出错: {e}")
            return []

# 定义向量库索引类
class VectorStoreIndex:
    def __init__(self, document_path: str, embed_model: EmbeddingModel, batch_size: int = 32) -> None:
        self.document_path = document_path
        self.embed_model = embed_model
        self.batch_size = batch_size
        self.vector_cache_path = f"{document_path}.pkl"
        self.documents = []
        self.vectors = []
        self.load_or_create_vectors()

    def load_or_create_vectors(self):
        if os.path.exists(self.vector_cache_path):
            with open(self.vector_cache_path, 'rb') as f:
                self.vectors = pickle.load(f)
            self.documents = [line.strip() for line in open(self.document_path, 'r', encoding='utf-8')]
        else:
            self.documents = [line.strip() for line in open(self.document_path, 'r', encoding='utf-8')]
            self.vectors = self.load_vectors_in_batches()
            with open(self.vector_cache_path, 'wb') as f:
                pickle.dump(self.vectors, f)

    def load_vectors_in_batches(self) -> List[List[float]]:
        vectors = []
        num_batches = (len(self.documents) + self.batch_size - 1) // self.batch_size

        # 添加进度条
        progress_bar = st.progress(0)
        
        for i in range(num_batches):
            batch_docs = self.documents[i * self.batch_size:(i + 1) * self.batch_size]
            batch_vectors = self.embed_model.get_embeddings(batch_docs)
            vectors.extend(batch_vectors)
            
            # 更新进度条
            progress = (i + 1) / num_batches
            progress_bar.progress(progress)
        
        progress_bar.empty()  # 清空进度条
        return vectors

    def get_similarity(self, vector1: List[float], vector2: List[float]) -> float:
        try:
            dot_product = np.dot(vector1, vector2)
            magnitude = np.linalg.norm(vector1) * np.linalg.norm(vector2)
            return dot_product / magnitude if magnitude else 0
        except Exception as e:
            st.error(f"计算相似度时出错: {e}")
            return 0

    def query(self, question: str, k: int = 1) -> List[str]:
        try:
            question_vector = self.embed_model.get_embeddings([question])[0]
            result = np.array([self.get_similarity(question_vector, vector) for vector in self.vectors])
            return np.array(self.documents)[result.argsort()[-k:][::-1]].tolist()
        except Exception as e:
            st.error(f"查询时出错: {e}")
            return []

# 每次用户选择名著时，加载对应的 knowledge 文档
embed_model_path = './AI-ModelScope/bge-small-zh-v1___5'
embed_model = EmbeddingModel(embed_model_path)
index = VectorStoreIndex(document_path, embed_model, batch_size=32)

# 初次运行时，session_state中没有"messages"，需要创建一个空列表
if "messages" not in st.session_state:
    st.session_state["messages"] = []

# 初始化 previous_book
if 'previous_book' not in st.session_state:
    st.session_state.previous_book = ""

# 清空对话历史当名著发生变化时
if st.session_state.previous_book != book_selection:
    st.session_state["messages"] = []
    st.session_state.previous_book = book_selection

# 每次对话时，遍历session_state中的所有消息，并显示在聊天界面上
for msg in st.session_state.messages:
    st.chat_message(msg["role"]).write(msg["content"])

# 如果用户在聊天输入框中输入了内容，则执行以下操作
if prompt := st.chat_input("请输入你的问题..."):
    try:
        # 将用户的输入添加到session_state中的messages列表中
        st.session_state.messages.append({"role": "user", "content": prompt})

        # 在聊天界面上显示用户的输入
        st.chat_message("user").write(prompt)

        # 使用索引查询与问题相关的上下文
        context = index.query(prompt)

        # 调用模型生成回复
        if context:
            full_prompt = f'背景：{context}\n问题：{prompt}\n请基于背景，回答问题。<sep>'
        else:
            full_prompt = prompt + "<sep>"

        inputs = tokenizer(full_prompt, return_tensors="pt")["input_ids"].cuda()
        outputs = model.generate(
            inputs,
            do_sample=True,
            temperature=0.7,
            top_k=50,
            top_p=0.95,
            max_length=1024
        )
        response = tokenizer.decode(outputs[0]).split("<sep>")[-1].replace("<eod>", '')

        # 将模型的输出添加到session_state中的messages列表中
        st.session_state.messages.append({"role": "assistant", "content": response})

        # 在聊天界面上显示模型的输出
        st.chat_message("assistant").write(response)
    except Exception as e:
        st.error(f"处理用户输入时出错: {e}")
