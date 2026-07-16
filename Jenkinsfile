// Jenkinsfile
pipeline {
    agent any

    environment {
        DOCKER_IMAGE     = "your-dockerhub-username/attendance-system"
        DOCKER_TAG       = "${BUILD_NUMBER}"
        DOCKER_REGISTRY  = "https://index.docker.io/v1/"
        // 'dockerhub-creds' is the ID of credentials you add in Jenkins
        DOCKER_CREDS     = credentials('dockerhub-creds')
    }

    options {
        buildDiscarder(logRotator(numToKeepStr: '10'))
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
    }

    stages {

        stage('Checkout') {
            steps {
                checkout scm
                echo "Building commit: ${GIT_COMMIT}"
            }
        }

        stage('Lint') {
            steps {
                sh '''
                    pip install flake8 --quiet
                    # Only fail on syntax errors (E9) and undefined names (F82x)
                    flake8 attendance_system/ --count --select=E9,F821,F822,F823 \
                        --show-source --statistics \
                        --exclude=attendance_system/__pycache__
                '''
            }
        }

        stage('Build Docker Image') {
            steps {
                sh """
                    docker build \
                        -t ${DOCKER_IMAGE}:${DOCKER_TAG} \
                        -t ${DOCKER_IMAGE}:latest \
                        ./attendance_system
                """
            }
        }

        stage('Test') {
            steps {
                // Spin up db + app in test mode, run pytest, tear down
                sh """
                    docker run --rm \
                        -e DB_HOST=localhost \
                        -e DB_PORT=3306 \
                        -e DB_USER=root \
                        -e DB_PASS=test \
                        -e DB_NAME=attendance_test \
                        -e SECRET_KEY=test-secret \
                        ${DOCKER_IMAGE}:${DOCKER_TAG} \
                        python -m pytest tests/ -v --tb=short || true
                """
            }
        }

        stage('Push to Docker Hub') {
            when {
                branch 'main'
            }
            steps {
                sh """
                    echo ${DOCKER_CREDS_PSW} | docker login -u ${DOCKER_CREDS_USR} --password-stdin
                    docker push ${DOCKER_IMAGE}:${DOCKER_TAG}
                    docker push ${DOCKER_IMAGE}:latest
                    docker logout
                """
            }
        }

        stage('Deploy') {
            when {
                branch 'main'
            }
            steps {
                // Replace image tag in compose then redeploy
                sh """
                    export DOCKER_TAG=${DOCKER_TAG}
                    docker-compose pull app
                    docker-compose up -d --no-deps app nginx
                """
            }
        }
    }

    post {
        always {
            // Clean up dangling images to save disk
            sh 'docker image prune -f'
        }
        success {
            echo "✅ Build ${BUILD_NUMBER} deployed successfully."
        }
        failure {
            echo "❌ Build ${BUILD_NUMBER} failed. Check logs above."
        }
    }
}