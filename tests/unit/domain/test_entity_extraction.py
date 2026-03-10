"""Unit tests for the entity extraction module."""

from ncms.domain.entity_extraction import extract_entity_names


class TestAPIPathExtraction:
    def test_simple_api_path(self):
        entities = extract_entity_names("Call GET /api/v2/users to fetch users")
        names = {e["name"] for e in entities}
        assert "/api/v2/users" in names

    def test_multiple_paths(self):
        text = "Routes: /auth/login and /api/v2/items/{id}"
        entities = extract_entity_names(text)
        names = {e["name"] for e in entities}
        assert "/auth/login" in names
        assert "/api/v2/items/{id}" in names

    def test_path_type_is_endpoint(self):
        entities = extract_entity_names("Endpoint /users/profile returns JSON")
        path_entities = [e for e in entities if e["type"] == "endpoint"]
        assert len(path_entities) >= 1


class TestPascalCaseExtraction:
    def test_pascal_case_component(self):
        entities = extract_entity_names("The UserService handles authentication")
        names = {e["name"] for e in entities}
        assert "UserService" in names

    def test_multi_word_pascal_case(self):
        entities = extract_entity_names("AuthTokenManager validates tokens")
        names = {e["name"] for e in entities}
        assert "AuthTokenManager" in names

    def test_type_is_component(self):
        entities = extract_entity_names("Initialize PaymentGateway first")
        comp = [e for e in entities if e["name"] == "PaymentGateway"]
        assert len(comp) == 1
        assert comp[0]["type"] == "component"

    def test_single_word_pascal_not_extracted(self):
        """Single capitalized words like 'Service' should not be extracted."""
        entities = extract_entity_names("The Service is running fine")
        names = {e["name"] for e in entities}
        assert "Service" not in names


class TestTechnologyExtraction:
    def test_known_technologies(self):
        text = "Stack: PostgreSQL database with Redis caching behind NGINX"
        entities = extract_entity_names(text)
        names = {e["name"] for e in entities}
        assert "PostgreSQL" in names
        assert "Redis" in names
        assert "NGINX" in names

    def test_case_insensitive_matching(self):
        entities = extract_entity_names("We use postgresql and react")
        names = {e["name"] for e in entities}
        # Should normalize to canonical casing
        assert "PostgreSQL" in names
        assert "React" in names

    def test_type_is_technology(self):
        entities = extract_entity_names("JWT authentication")
        jwt = [e for e in entities if e["name"] == "JWT"]
        assert len(jwt) == 1
        assert jwt[0]["type"] == "technology"


class TestTableExtraction:
    def test_table_keyword_before_name(self):
        entities = extract_entity_names("Added a required column to table users")
        names = {e["name"] for e in entities}
        assert "users" in names

    def test_name_before_table_keyword(self):
        entities = extract_entity_names("The events table has 3 columns")
        names = {e["name"] for e in entities}
        assert "events" in names

    def test_sql_keywords_not_extracted_as_tables(self):
        """SQL keywords like SELECT, FROM should not become table entities."""
        entities = extract_entity_names("SELECT * FROM users WHERE active = TRUE")
        names = {e["name"] for e in entities}
        assert "SELECT" not in names
        assert "FROM" not in names
        assert "WHERE" not in names
        assert "TRUE" not in names


class TestDeduplication:
    def test_case_insensitive_dedup(self):
        """Same entity mentioned with different casing should appear once."""
        text = "UserService is great. The userservice handles login."
        entities = extract_entity_names(text)
        user_service = [e for e in entities if e["name"].lower() == "userservice"]
        assert len(user_service) == 1

    def test_tech_and_pascal_dedup(self):
        """A PascalCase name that's also a tech should not be double-counted."""
        entities = extract_entity_names("Use NetworkX for the graph")
        nx = [e for e in entities if e["name"].lower() == "networkx"]
        assert len(nx) == 1


class TestEdgeCases:
    def test_empty_text(self):
        assert extract_entity_names("") == []

    def test_short_text(self):
        assert extract_entity_names("x") == []

    def test_no_entities(self):
        entities = extract_entity_names("The weather is nice today")
        # Should be empty or very minimal
        assert len(entities) <= 1

    def test_max_entities_cap(self):
        """Should not return more than 20 entities."""
        # Create text with many extractable entities
        techs = "PostgreSQL Redis NGINX Docker Kubernetes React Vue Angular "
        techs += "Express FastAPI Django Flask JWT OAuth Kafka RabbitMQ "
        techs += "Celery Terraform Ansible Vercel Netlify AWS GCP Azure "
        entities = extract_entity_names(techs)
        assert len(entities) <= 20

    def test_realistic_content(self):
        """Full realistic memory content should extract meaningful entities."""
        text = (
            "The UserService exposes GET /api/v2/users which returns paginated "
            "results. It uses PostgreSQL for storage with PgBouncer connection "
            "pooling. JWT tokens authenticate each request."
        )
        entities = extract_entity_names(text)
        names = {e["name"] for e in entities}
        assert "UserService" in names
        assert "/api/v2/users" in names
        assert "PostgreSQL" in names
        assert "PgBouncer" in names
        assert "JWT" in names
        assert len(entities) >= 4


class TestDottedNames:
    def test_dotted_module_name(self):
        entities = extract_entity_names("Import from react.query for data fetching")
        names = {e["name"] for e in entities}
        assert "react.query" in names

    def test_dotted_type_is_module(self):
        entities = extract_entity_names("Using shadcn.ui components")
        mods = [e for e in entities if e["type"] == "module"]
        assert any("shadcn.ui" == m["name"] for m in mods)
