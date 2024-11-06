    import streamlit as st
    # Set page config must be the first Streamlit command
    st.set_page_config(page_title="Quartr Data Retrieval", page_icon="📊", layout="wide")

    # Import tasks first
    from tasks import huey, process_files_task, process_single_file
    from utils import QuartrAPI, TranscriptProcessor, S3Handler

    # Then other imports
    from streamlit_autorefresh import st_autorefresh
    import boto3
    import requests
    import json
    from datetime import datetime
    import pandas as pd
    import asyncio
    import aiohttp
    import aioboto3
    from typing import List, Dict, Any
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    import os
    from dotenv import load_dotenv

    # Load environment variables
    load_dotenv()

    # Function to get environment variables
    def get_env_variable(key: str) -> str:
        return os.getenv(key)

    # Environment variables configuration
    QUARTR_API_KEY = get_env_variable("QUARTR_API_KEY")
    AWS_ACCESS_KEY_ID = get_env_variable("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = get_env_variable("AWS_SECRET_ACCESS_KEY")
    AWS_DEFAULT_REGION = get_env_variable("AWS_DEFAULT_REGION")
    DEFAULT_S3_BUCKET = get_env_variable("DEFAULT_S3_BUCKET")
    REDIS_URL = get_env_variable("REDIS_URL")

    # Initialize session state
    if "processing_complete" not in st.session_state:
        st.session_state.processing_complete = False
    if "processed_files" not in st.session_state:
        st.session_state.processed_files = []

    # Auto refresh when task is running
    if "task_id" in st.session_state:
        st_autorefresh(interval=10000)  # Refresh every 10 seconds

    def check_task_status():
        if "task_id" in st.session_state:
            try:
                # Create persistent containers if they don't exist
                if 'progress_container' not in st.session_state:
                    st.session_state.progress_container = st.empty()
                if 'status_container' not in st.session_state:
                    st.session_state.status_container = st.empty()
                if 'results_container' not in st.session_state:
                    st.session_state.results_container = st.empty()

                # Get task result
                result = huey.result(st.session_state.task_id)
                
                if result:
                    if result.get('status') == 'In Progress':
                        # Update progress bar
                        total = result.get('total', 0)
                        processed = result.get('processed', 0)
                        progress = processed / total if total > 0 else 0
                        st.session_state.progress_container.progress(progress)

                        # Update status message
                        st.session_state.status_container.text(
                            f"Processing files: {processed}/{total}"
                        )

                        # Update results
                        success = result.get('success', 0)
                        failed = result.get('failed', 0)
                        st.session_state.results_container.text(
                            f"Successful uploads: {success}\n"
                            f"Failed uploads: {failed}"
                        )

                    elif result.get('status') == 'Complete':
                        # Show completed progress bar
                        st.session_state.progress_container.progress(1.0)
                        
                        # Show completion message
                        st.session_state.status_container.text("Processing complete!")
                        
                        # Show final results
                        st.session_state.results_container.text(
                            f"Final results:\n"
                            f"Total files processed: {result.get('total', 0)}\n"
                            f"Successful uploads: {result.get('success', 0)}\n"
                            f"Failed uploads: {result.get('failed', 0)}"
                        )
                        
                        # Clear task ID from session state
                        del st.session_state["task_id"]

                    elif result.get('status') == 'Failed':
                        st.error(f"Processing failed: {result.get('error', 'Unknown error')}")
                        del st.session_state["task_id"]

            except Exception as e:
                st.error(f"Error checking task status: {str(e)}")
                if "task_id" in st.session_state:
                    del st.session_state["task_id"]

    def test_queue_connection():
        """Test the queue connection with better error handling and feedback"""
        try:
            from tasks import ping
            from huey.exceptions import TaskException
            
            with st.spinner('Testing queue connection...'):
                # Enqueue the task
                task = ping()
                
                # Wait for result with timeout
                try:
                    response = task.get(blocking=True, timeout=15)  # Increased timeout
                    if response == "pong":
                        return True, "Queue connection successful!"
                    else:
                        return False, f"Unexpected response from queue: {response}"
                except TaskException as e:
                    return False, f"Task execution failed: {str(e)}"
                except TimeoutError:
                    return False, "Queue connection timed out. Worker might be unavailable."
        except Exception as e:
            return False, f"Queue connection failed: {str(e)}"

    @st.cache_data(ttl=60)
    def health_check():
        return "OK"

    def init_routes():
        if st.query_params.get("health") == "check":
            st.write(health_check())
            st.stop()

    def main():
        init_routes()
        st.title("Quartr Data Retrieval and S3 Upload")

        # Validate environment variables
        if not all([
            QUARTR_API_KEY,
            AWS_ACCESS_KEY_ID,
            AWS_SECRET_ACCESS_KEY,
            AWS_DEFAULT_REGION,
            REDIS_URL,
        ]):
            st.error("""
            Missing required environment variables. Please ensure all required environment variables are set.
            """)
            return

        # Check task status if task is running
        check_task_status()

        # Only show form if no task is running
        if "task_id" not in st.session_state:
            with st.form(key="quartr_form"):
                isin_input = st.text_area(
                    "Enter ISINs (one per line)",
                    height=100,
                )

                col1, col2 = st.columns(2)
                with col1:
                    start_date = st.date_input(
                        "Start Date",
                        datetime(2024, 1, 1),
                        help="Select start date for document retrieval",
                        min_value=datetime(2000, 1, 1),
                    )
                with col2:
                    end_date = st.date_input(
                        "End Date",
                        datetime(2024, 12, 31),
                        help="Select end date for document retrieval",
                        max_value=datetime(2025, 12, 31),
                    )

                doc_types = st.multiselect(
                    "Select document types",
                    ["slides", "report", "transcript", "audio"],
                    default=["slides", "report", "transcript", "audio"],
                )

                s3_bucket = st.text_input(
                    "S3 Bucket Name",
                    value=DEFAULT_S3_BUCKET or "",
                )

                submitted = st.form_submit_button("Start Processing")

                if submitted:
                    if not isin_input or not s3_bucket or not doc_types:
                        st.error("Please fill in all required fields")
                        return

                    if start_date > end_date:
                        st.error("Start date must be before end date")
                        return

                    isin_list = [isin.strip() for isin in isin_input.split("\n") if isin.strip()]
                    if not isin_list:
                        st.error("Please enter at least one valid ISIN")
                        return

                    try:
                        # Start background task with Huey
                        task = process_files_task(
                            isin_list,
                            start_date.strftime("%Y-%m-%d"),
                            end_date.strftime("%Y-%m-%d"),
                            doc_types,
                            s3_bucket,
                        )
                        # Store task ID in session state
                        st.session_state.task_id = task.id
                        st.experimental_rerun()
                    except Exception as e:
                        st.error(f"An error occurred: {str(e)}")
                        return

        # Add Queue connection test button
        if st.button("Test Queue Connection"):
            success, message = test_queue_connection()
            if success:
                st.success(message)
            else:
                st.error(message)

    if __name__ == "__main__":
        main()    
