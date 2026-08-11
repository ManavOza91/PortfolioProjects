-- GENERATED FILE — do not edit by hand.
-- Regenerate with: ./run.sh export-portable
-- Source of truth: data/taxonomy.yaml, config.yaml, signal_engine/
--
-- SQLite schema — what the reference Python implementation runs on.

CREATE TABLE buddy_snapshots (
	id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	period_type VARCHAR(20) NOT NULL, 
	period_start DATE NOT NULL, 
	period_end DATE NOT NULL, 
	score FLOAT NOT NULL, 
	progression FLOAT NOT NULL, 
	signal_quality FLOAT NOT NULL, 
	volume FLOAT NOT NULL, 
	consistency FLOAT NOT NULL, 
	detail TEXT, 
	owner VARCHAR(120), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_buddy_snapshot_period UNIQUE (tenant_id, owner, period_type, period_start)
);

CREATE INDEX ix_buddy_snapshots_period_start ON buddy_snapshots (period_start);
CREATE INDEX ix_buddy_snapshots_tenant_id ON buddy_snapshots (tenant_id);

CREATE TABLE companies (
	id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	name VARCHAR(300) NOT NULL, 
	domain VARCHAR(200), 
	country VARCHAR(100), 
	segment VARCHAR(100), 
	size_band VARCHAR(50), 
	product_fit VARCHAR(10), 
	current_score FLOAT NOT NULL, 
	tier VARCHAR(10), 
	score_computed_at DATETIME, 
	first_signal_date DATE, 
	last_signal_date DATE, 
	status VARCHAR(30) NOT NULL, 
	notes TEXT, 
	created_by VARCHAR(120), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_companies_status CHECK (status IN ('watchlist','active','engaged','won','lost','dormant'))
);

CREATE INDEX ix_companies_tenant_id ON companies (tenant_id);
CREATE INDEX ix_companies_tenant_name ON companies (tenant_id, name);
CREATE INDEX ix_companies_tenant_score ON companies (tenant_id, current_score);

CREATE TABLE manager_insights (
	id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	raw_text TEXT NOT NULL, 
	parsed_segment VARCHAR(200), 
	parsed_geography VARCHAR(200), 
	parsed_signal_type VARCHAR(80), 
	parsed_summary TEXT, 
	weight_adjustment FLOAT NOT NULL, 
	author VARCHAR(120), 
	created_date DATE NOT NULL, 
	expiry_date DATE, 
	performance_score FLOAT, 
	surfaced_count INTEGER NOT NULL, 
	converted_count INTEGER NOT NULL, 
	active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_manager_insights_tenant_id ON manager_insights (tenant_id);

CREATE TABLE signal_types (
	id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	"key" VARCHAR(80) NOT NULL, 
	label VARCHAR(200) NOT NULL, 
	product_fit VARCHAR(10) NOT NULL, 
	base_weight FLOAT NOT NULL, 
	sources VARCHAR(20) NOT NULL, 
	decays BOOLEAN NOT NULL, 
	description TEXT NOT NULL, 
	examples TEXT NOT NULL, 
	active BOOLEAN NOT NULL, 
	sort_order INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_signal_types_tenant_key UNIQUE (tenant_id, "key"), 
	CONSTRAINT ck_signal_types_product_fit CHECK (product_fit IN ('A','B','both')), 
	CONSTRAINT ck_signal_types_sources CHECK (sources IN ('manual','auto','both'))
);

CREATE INDEX ix_signal_types_tenant_id ON signal_types (tenant_id);

CREATE TABLE contacts (
	id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	company_id INTEGER NOT NULL, 
	full_name VARCHAR(200), 
	job_title VARCHAR(250), 
	email VARCHAR(250), 
	linkedin_url VARCHAR(500), 
	country VARCHAR(100), 
	persona VARCHAR(100), 
	source VARCHAR(60), 
	date_added DATE NOT NULL, 
	notes TEXT, 
	created_by VARCHAR(120), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
);

CREATE INDEX ix_contacts_company_id ON contacts (company_id);
CREATE INDEX ix_contacts_tenant_company ON contacts (tenant_id, company_id);
CREATE INDEX ix_contacts_tenant_id ON contacts (tenant_id);

CREATE TABLE score_snapshots (
	id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	company_id INTEGER NOT NULL, 
	score FLOAT NOT NULL, 
	tier VARCHAR(10), 
	signal_count INTEGER NOT NULL, 
	compounding_applied BOOLEAN NOT NULL, 
	breakdown TEXT, 
	snapshot_date DATE NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_score_snapshot_company_date UNIQUE (company_id, snapshot_date), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
);

CREATE INDEX ix_score_snapshots_company_id ON score_snapshots (company_id);
CREATE INDEX ix_score_snapshots_snapshot_date ON score_snapshots (snapshot_date);
CREATE INDEX ix_score_snapshots_tenant_id ON score_snapshots (tenant_id);

CREATE TABLE signals (
	id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	company_id INTEGER NOT NULL, 
	type_key VARCHAR(80), 
	weight FLOAT NOT NULL, 
	product_fit VARCHAR(10), 
	source VARCHAR(30) NOT NULL, 
	raw_text TEXT, 
	parsed_summary TEXT, 
	confidence FLOAT NOT NULL, 
	timeline VARCHAR(200), 
	detected_date DATE NOT NULL, 
	expiry_date DATE, 
	status VARCHAR(20) NOT NULL, 
	outcome_tag VARCHAR(60), 
	detail_url VARCHAR(500), 
	created_by VARCHAR(120), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_signals_source CHECK (source IN ('auto','manual','manager_insight','inbound','event')), 
	CONSTRAINT ck_signals_status CHECK (status IN ('scored','review','unscored','rejected')), 
	CONSTRAINT ck_signals_confidence CHECK (confidence >= 0 AND confidence <= 1), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
);

CREATE INDEX ix_signals_company_detected ON signals (company_id, detected_date);
CREATE INDEX ix_signals_company_id ON signals (company_id);
CREATE INDEX ix_signals_status ON signals (status);
CREATE INDEX ix_signals_tenant_id ON signals (tenant_id);
CREATE INDEX ix_signals_type_key ON signals (type_key);

CREATE TABLE activity (
	id INTEGER NOT NULL, 
	tenant_id INTEGER NOT NULL, 
	company_id INTEGER NOT NULL, 
	contact_id INTEGER, 
	type VARCHAR(30) NOT NULL, 
	direction VARCHAR(10) NOT NULL, 
	date DATE NOT NULL, 
	outcome VARCHAR(60), 
	notes TEXT, 
	signal_id_at_time_of_contact INTEGER, 
	company_tier_at_time_of_contact VARCHAR(10), 
	company_score_at_time_of_contact FLOAT, 
	created_by VARCHAR(120), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_activity_type CHECK (type IN ('email','call','linkedin','meeting','reply','note')), 
	CONSTRAINT ck_activity_direction CHECK (direction IN ('out','in')), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE, 
	FOREIGN KEY(contact_id) REFERENCES contacts (id) ON DELETE SET NULL, 
	FOREIGN KEY(signal_id_at_time_of_contact) REFERENCES signals (id) ON DELETE SET NULL
);

CREATE INDEX ix_activity_company_id ON activity (company_id);
CREATE INDEX ix_activity_contact_id ON activity (contact_id);
CREATE INDEX ix_activity_date ON activity (date);
CREATE INDEX ix_activity_tenant_date ON activity (tenant_id, date);
CREATE INDEX ix_activity_tenant_id ON activity (tenant_id);
