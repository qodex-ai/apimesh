from langchain_text_splitters import RecursiveCharacterTextSplitter, Language
from langchain.vectorstores import FAISS
from llm_client import OpenAiClient
from utils import num_tokens_from_string


class GenerateFaissIndex:
    def __init__(self):
        self.openai_client = OpenAiClient()

    def create_faiss_index(self, file_paths, framework):
        if framework == "ruby_on_rails":
            text_splitter = RecursiveCharacterTextSplitter.from_language(
                chunk_size=2000,
                chunk_overlap=200, language=Language.RUBY
            )
        elif framework == "express":
            text_splitter = RecursiveCharacterTextSplitter.from_language(
                chunk_size=2000,
                chunk_overlap=200, language=Language.JS
            )
        elif framework == "django" or framework == "flask" or framework == "fastapi":
            text_splitter = RecursiveCharacterTextSplitter.from_language(
                chunk_size=2000,
                chunk_overlap=200, language=Language.PYTHON
            )
        elif framework == "golang":
            text_splitter = RecursiveCharacterTextSplitter.from_language(
                chunk_size=2000,
                chunk_overlap=200, language=Language.GO
            )
        else:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=2000,
                chunk_overlap=200
            )
        texts = []
        metadata = []

        for file_path in file_paths:
            try:
                with open(file_path, 'r', encoding='utf-8') as handle:
                    file_content = handle.read()
            except (OSError, UnicodeDecodeError) as ex:
                print(f"apimesh: skipping unreadable file {file_path}: {ex}")
                continue
            chunks = text_splitter.split_text(file_content)
            texts.extend(chunks)
            metadata.extend([{'file_path': str(file_path)}] * len(chunks))

        if not texts:
            raise ValueError("No readable source text found to build the search index from.")

        all_indices = []
        batch = []
        batch_meta = []
        batch_token_count = 0

        for text, meta in zip(texts, metadata):
            tokens = num_tokens_from_string(text)

            # Start new batch if adding this text exceeds token limit
            if batch_token_count + tokens > 290000:
                index = FAISS.from_texts(batch, self.openai_client.embeddings, metadatas=batch_meta)
                all_indices.append(index)
                batch, batch_meta, batch_token_count = [], [], 0

            batch.append(text)
            batch_meta.append(meta)
            batch_token_count += tokens

        # Final batch
        if batch:
            index = FAISS.from_texts(batch, self.openai_client.embeddings, metadatas=batch_meta)
            all_indices.append(index)

        # Merge all indices. Returning the merged batches is the whole point of
        # batching: re-embedding the full corpus here would double the spend and
        # reintroduce the per-request token limit the batches avoid.
        final_index = all_indices[0]
        for idx in all_indices[1:]:
            final_index.merge_from(idx)
        return final_index

    @staticmethod
    def get_authentication_related_information(faiss_vector_db):
        query = "function to handle authentication information and authorization information"
        docs = faiss_vector_db.similarity_search(str(query), k=4)
        content_list = [doc.page_content.strip() for doc in docs]
        return content_list
