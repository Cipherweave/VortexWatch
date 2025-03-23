from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from openai import OpenAI
import cohere
import json
import os
from dotenv import load_dotenv
from duckduckgo_search import DDGS
import concurrent.futures
import threading

app = Flask(__name__)
CORS(app)

load_dotenv()
openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
cohere_client = cohere.Client(api_key=os.getenv('COHERE_API_KEY'))
ASSISTANT_ID = "asst_v2se6YGN5d3xm4voj2k8eMOb"

# Create a thread pool executor
executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        data = request.get_json()
        domain = data.get("domain")
        
        if not domain:
            return jsonify({"error": "Domain is required"}), 400
        
        # Skip browser-specific URLs
        if domain in ['newtab', 'chrome://newtab', 'about:blank'] or domain.startswith(('chrome://', 'about:', 'edge://')):
            return jsonify({
                "status": "error",
                "message": "Cannot analyze browser-specific pages",
                "domain": domain
            }), 400
        
        # Ensure domain has proper scheme
        if not domain.startswith(('http://', 'https://')):
            domain = 'https://' + domain
            
        # Step 1: Get privacy policy URL
        privacy_url = get_privacy_policy_url(domain)
        
        if not privacy_url:
            return jsonify({
                "status": "error",
                "message": "Privacy policy not found",
                "domain": domain
            }), 404
            
        # Step 2: Check policy safety using a thread
        policy_future = executor.submit(policy_check, privacy_url)
        
        # Wait for the result with a timeout
        try:
            policy_results = policy_future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            return jsonify({
                "status": "error",
                "message": "Policy analysis timed out",
                "domain": domain
            }), 504
        
        is_safe = policy_results[0] == "Policy Safe!"
        
        response_data = {
            "status": "success",
            "domain": domain,
            "privacy_url": privacy_url,
            "is_safe": is_safe,
            "policy_analysis": policy_results
        }
        
        # Step 3: If not safe, get alternative sites in parallel
        if not is_safe:
            # Extract company name from domain
            company_name = domain.split('//')[-1].split('.')[0]
            if company_name in ['www', 'app', 'web']:
                company_name = domain.split('//')[-1].split('.')[1]
                
            # Get related websites in a separate thread
            related_future = executor.submit(get_related_websites, company_name)
            
            try:
                related_sites = related_future.result(timeout=15)
                # Get official URLs in a separate thread
                urls_future = executor.submit(get_official_urls, related_sites)
                official_urls = urls_future.result(timeout=15)
                response_data["alternatives"] = official_urls
            except concurrent.futures.TimeoutError:
                response_data["alternatives"] = {"error": "Retrieving alternatives timed out"}
            
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def get_related_websites(company_name: str) -> list:
    prompt = (f"Provide a list of exactly three websites or apps that are similar to {company_name}."  
            f' Rules: '
              f'- Only return the names of the websites or apps. '
              f'- No explanations, descriptions, or extra words.  '
              f'- The response must contain exactly three names.'
              f'- Do not include unrelated websites.  ')

    response = cohere_client.generate(
        model="command",
        prompt=prompt,
        max_tokens=50,
        temperature=0.7
    )

    suggestions = response.generations[0].text.strip().split("\n")

    lst = []
    for s in suggestions:
        if s:
            lst.append(s)

        if len(lst) == 3:
            break

    return lst

def extract_text(url):
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")

        # Extract headings and paragraphs
        texts = []
        for tag in soup.find_all(["h1", "h2", "h3", "p"]):
            text = tag.get_text(strip=True)
            if text:  # Only add non-empty text
                texts.append(text)

        if not texts:
            return "No readable text found in the privacy policy."
            
        return "\n".join(texts)  # Combine into a readable format
    except Exception as e:
        print(f"Error extracting text: {e}")
        return "Error extracting text from the policy page."


def policy_check(url):
    try:
        lst = []
        text = extract_text(url)
        
        # Check if text is empty or too short
        if not text or text == "Error extracting text from the policy page." or len(text) < 10:
            return ["Privacy Concerns Detected", "Could not extract meaningful text from the privacy policy."]
        
        # Limit text length to avoid token limits
        if len(text) > 8000:
            text = text[:8000]
            
        thread = openai_client.beta.threads.create()

        # Send the URL as a message to the assistant
        openai_client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=text
        )

        # Run the assistant on the thread
        run = openai_client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=ASSISTANT_ID
        )

        # Wait for the assistant to respond
        while run.status not in ["completed", "failed"]:
            run = openai_client.beta.threads.runs.retrieve(
                thread_id=thread.id,
                run_id=run.id
            )

        if run.status == "failed":
            return ["Analysis failed", "Could not analyze the policy"]
            
        # Retrieve the assistant's response
        messages = openai_client.beta.threads.messages.list(thread_id=thread.id)
        assistant_response = messages.data[0].content[0].text.value

        lst.append(assistant_response)

        if assistant_response == "Policy Safe!":
            return lst
       
        # Send the URL as a message to the assistant
        openai_client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content="Elaborate with quote"
        )

        # Run the assistant on the thread
        run = openai_client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=ASSISTANT_ID
        )

        # Wait for the assistant to respond
        while run.status not in ["completed", "failed"]:
            run = openai_client.beta.threads.runs.retrieve(
                thread_id=thread.id,
                run_id=run.id
            )

        if run.status == "failed":
            lst.append("Could not elaborate on the analysis")
            return lst

        # Retrieve the assistant's response
        messages = openai_client.beta.threads.messages.list(thread_id=thread.id)
        assistant_response = messages.data[0].content[0].text.value

        lst.append(assistant_response)

        return lst
    except Exception as e:
        print(f"Error in policy check: {e}")
        return ["Error analyzing policy", str(e)]

def get_privacy_policy_url(website_url):
    try:
        response = requests.get(website_url, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Common privacy policy terms to look for
        privacy_terms = ['privacy', 'privacy policy', 'data policy', 'data protection']
        
        # Look for links that might have privacy policy url
        links = soup.find_all(["a", "span", "button", "div"], href=True)
        for link in links:
            href = link.get('href', '')
            text = link.text.lower()

            # Check both href and link text for privacy-related terms
            if any(term in href.lower() for term in privacy_terms) or any(term in text for term in privacy_terms):
                # join URLs using urljoin
                absolute_url = urljoin(website_url, href)
                return absolute_url
                
        # If not found in primary search, look in footer elements
        footer = soup.find_all(["footer", "div", "nav"], class_=lambda c: c and ('footer' in c.lower() or 'bottom' in c.lower()))
        for element in footer:
            links = element.find_all('a', href=True)
            for link in links:
                href = link.get('href', '')
                text = link.text.lower()
                
                if any(term in href.lower() for term in privacy_terms) or any(term in text for term in privacy_terms):
                    absolute_url = urljoin(website_url, href)
                    return absolute_url
                    
        return None
    except Exception as e:
        print(f"Error fetching privacy policy: {e}")
        return None

def get_official_urls(company_names):
    urls = {}
    try:
        with DDGS() as ddgs:
            for company in company_names:
                query = (f"{company} official website, no account or log in page just"
                         f"the official website")
                search_results = ddgs.text(query, max_results=1)

                if search_results:
                    urls[company] = search_results[0]["href"]
                else:
                    urls[company] = "URL not found"

        return urls
    except Exception as e:
        print(f"Error getting official URLs: {e}")
        return {company: "Error retrieving URL" for company in company_names}

@app.route("/")
def index():
    return jsonify({"status": "API is running", "endpoints": ["/analyze"]})

if __name__ == "__main__":
    app.run(debug=True, threaded=True)
