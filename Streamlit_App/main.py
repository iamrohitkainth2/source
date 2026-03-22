# import os
# import streamlit as st
# import pickle
# import time
# from langchain_openai import OpenAI
# from langchain_classic.chains import RetrievalQAWithSourcesChain
# # from langchain_classic.chains import RetrievalQA
# # from langchain_classic.chains import create_retrieval_chain
# # from langchain_classic.chains import create_retrieval_chain
# from langchain_text_splitters import RecursiveCharacterTextSplitter
# from langchain_community.document_loaders import UnstructuredURLLoader
# from langchain_community.embeddings import OpenAIEmbeddings
# from langchain_community.vectorstores import FAISS

import os
import streamlit as st
import time
from langchain.chains import RetrievalQAWithSourcesChain
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.document_loaders import UnstructuredURLLoader, WebBaseLoader
from langchain.embeddings import OpenAIEmbeddings
from langchain.vectorstores import FAISS
from langchain.chat_models import AzureChatOpenAI
import sys
print(f"System executable: {sys.executable}")
from dotenv import load_dotenv
load_dotenv()  # take environment variables from .env (especially openai api key)

azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "") or os.getenv("OPENAI_API_BASE", "")
if azure_endpoint and not azure_endpoint.endswith("/"):
    azure_endpoint = azure_endpoint + "/"

azure_api_key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")

# Older LangChain/OpenAI integrations still check these env vars even for Azure mode.
if azure_api_key and not os.getenv("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = azure_api_key
if azure_endpoint and not os.getenv("OPENAI_API_BASE"):
    os.environ["OPENAI_API_BASE"] = azure_endpoint
if not os.getenv("OPENAI_API_TYPE"):
    os.environ["OPENAI_API_TYPE"] = "azure"
if not os.getenv("OPENAI_API_VERSION"):
    os.environ["OPENAI_API_VERSION"] = "2023-05-15"

if not (azure_api_key or os.getenv("OPENAI_API_KEY")):
    st.error("Missing API credentials. Set AZURE_OPENAI_API_KEY (preferred) or OPENAI_API_KEY.")
    st.stop()

st.title("RockyBot: News Research Tool 📈")
st.sidebar.title("News Article URLs")

urls = []
for i in range(3):
    url = st.sidebar.text_input(f"URL {i+1}")
    urls.append(url)

process_url_clicked = st.sidebar.button("Process URLs")
file_path = "faiss_store_openai.pkl"

main_placeholder = st.empty()
# llm = OpenAI(temperature=0.9, max_tokens=500)
llm = AzureChatOpenAI(
    deployment_name="gpt-4o",
    openai_api_version="2023-03-15-preview",
    openai_api_base=azure_endpoint,
    openai_api_key=azure_api_key,
    openai_api_type="azure"
)

if process_url_clicked:
    error_markers = [
        " 410 gone",
        "\n410 gone",
        "http error 410",
        " 404 not found",
        "\n404 not found",
        "access denied",
        "forbidden",
        "captcha",
        "bot verification",
    ]

    def is_error_like_document(text):
        normalized = f" {text.lower().strip()}"
        return any(marker in normalized for marker in error_markers)

    input_urls = [url.strip() for url in urls if url and url.strip()]
    if not input_urls:
        st.error("Please provide at least one valid URL before processing.")
        st.stop()

    # load data
    loader = UnstructuredURLLoader(urls=input_urls)
    main_placeholder.text("Data Loading...Started...✅✅✅")
    data = loader.load()

    # Keep only documents that actually contain readable text.
    data = [doc for doc in data if getattr(doc, "page_content", "").strip()]

    # Remove obvious HTTP-error or bot-block pages from loaded content.
    removed_sources = []
    filtered_data = []
    for doc in data:
        page_text = getattr(doc, "page_content", "")
        if is_error_like_document(page_text):
            source = "Unknown source"
            if getattr(doc, "metadata", None):
                source = doc.metadata.get("source", "Unknown source")
            removed_sources.append(source)
        else:
            filtered_data.append(doc)
    data = filtered_data

    # Fallback loader for pages that fail Unstructured parsing in Azure.
    if not data:
        main_placeholder.text("Primary loader returned no content. Trying fallback loader...")
        fallback_docs = []
        for source_url in input_urls:
            try:
                docs_from_url = WebBaseLoader([source_url]).load()
                fallback_docs.extend(
                    [doc for doc in docs_from_url if getattr(doc, "page_content", "").strip()]
                )
            except Exception as exc:
                st.warning(f"Could not load URL via fallback loader: {source_url}. Error: {exc}")
        data = fallback_docs

    if not data:
        st.error(
            "No readable content could be extracted from the provided URL(s). "
            "Try different article URLs that return standard HTML content."
        )
        st.stop()

    if removed_sources:
        unique_sources = list(dict.fromkeys(removed_sources))
        st.warning(
            "Skipped potential error pages: "
            + ", ".join(unique_sources)
        )

    # split data
    text_splitter = RecursiveCharacterTextSplitter(
        separators=['\n\n', '\n', '.', ','],
        chunk_size=1000
    )
    main_placeholder.text("Text Splitter...Started...✅✅✅")
    docs = text_splitter.split_documents(data)
    docs = [doc for doc in docs if getattr(doc, "page_content", "").strip()]
    if not docs:
        st.error("No text chunks were produced from the fetched URLs.")
        st.stop()

    st.subheader("Loaded Chunks")
    st.write(f"Total chunks: {len(docs)}")
    with st.expander("View chunk previews", expanded=True):
        for idx, doc in enumerate(docs, start=1):
            source = "Unknown source"
            if getattr(doc, "metadata", None):
                source = doc.metadata.get("source", "Unknown source")

            preview = doc.page_content[:400].replace("\n", " ").strip()
            if len(doc.page_content) > 400:
                preview += "..."

            st.markdown(f"**Chunk {idx}** - `{source}`")
            st.write(preview)

    # create embeddings and save it to FAISS index
    # embeddings = OpenAIEmbeddings()
    embeddings = OpenAIEmbeddings(
    model="text-embedding-3-large",
    deployment="text-embedding-3-large",
    openai_api_version="2023-05-15",
    openai_api_base=azure_endpoint,
    openai_api_key=azure_api_key,
    openai_api_type="azure"
    )
    print("Documents after splitting:")
    print(docs)

    # Build index incrementally to avoid SDK batching issues on older OpenAI/LangChain combos.
    vectorstore_openai = None
    for idx, doc in enumerate(docs, start=1):
        if vectorstore_openai is None:
            vectorstore_openai = FAISS.from_documents([doc], embeddings)
        else:
            vectorstore_openai.add_documents([doc])
        if idx % 10 == 0 or idx == len(docs):
            main_placeholder.text(f"Embedding Vector Building...{idx}/{len(docs)} chunks")

    main_placeholder.text("Embedding Vector Started Building...✅✅✅")
    time.sleep(2)


    # Save the FAISS index to a pickle file
    vectorstore_openai.save_local(file_path)
    # with open(file_path, "wb") as f:
    #     pickle.dump(vectorstore_openai, f)

query = main_placeholder.text_input("Question: ")
if query:
    if os.path.exists(file_path):
            embeddings = OpenAIEmbeddings(
            model="text-embedding-3-large",
            deployment="text-embedding-3-large",
            openai_api_version="2023-05-15",
            openai_api_base=azure_endpoint,
            openai_api_key=azure_api_key,
            openai_api_type="azure"
            )
            # Support both old and new LangChain FAISS.load_local signatures.
            try:
                vectorstore = FAISS.load_local(
                    file_path,
                    embeddings,
                    allow_dangerous_deserialization=True,
                )
            except TypeError:
                vectorstore = FAISS.load_local(file_path, embeddings)
            # Avoid old LangChain map_reduce token-usage merge bug with Azure/OpenAI responses.
            try:
                chain = RetrievalQAWithSourcesChain.from_chain_type(
                    llm=llm,
                    chain_type="stuff",
                    retriever=vectorstore.as_retriever(),
                )
            except Exception:
                # Fallback for older variants that may not expose from_chain_type.
                chain = RetrievalQAWithSourcesChain.from_llm(
                    llm=llm,
                    retriever=vectorstore.as_retriever(),
                )
            result = chain({"question": query}, return_only_outputs=True)
            # result will be a dictionary of this format --> {"answer": "", "sources": [] }
            st.header("Answer")
            st.write(result["answer"])

            # Display sources, if available
            sources = result.get("sources", "")
            if sources:
                st.subheader("Sources:")
                sources_list = sources.split("\n")  # Split the sources by newline
                for source in sources_list:
                    st.write(source)




