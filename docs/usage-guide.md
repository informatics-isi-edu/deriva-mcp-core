# Usage Guide

Natural language prompts for interacting with DERIVA via an LLM client connected
to `deriva-mcp-core`. All examples assume the MCP server is connected and
`DERIVA_MCP_DISABLE_MUTATING_TOOLS=false` where write operations are shown.

Replace `localhost` and catalog/schema/table names with values from your deployment.

---

## Schema Exploration

**Catalog overview:**

```
List all the schemas in catalog 1 on localhost
```

```
What tables are in the isa schema of catalog 1 on localhost? Show me their columns.
```

```
Get the full definition for the isa:Dataset table including foreign keys
```

```
What foreign keys reference isa:Dataset?
```

**Snapshot history:**

```
What is the earliest available snapshot for catalog 1 on localhost?
```

```
Resolve the snapshot identifier 2TA-YA2D-ZDWY for catalog 1 on localhost
```

---

## Entity CRUD

**Fetching rows:**

```
Show me the first 10 rows from isa:Dataset in catalog 1 on localhost
```

```
Get all released datasets from isa:Dataset in catalog 1 on localhost
```

```
How many rows are in isa:Replicate in catalog 1 on localhost?
```

```
Get the next page of results after RID 1-ABCD from isa:Dataset in catalog 1 on localhost
```

(cursor pagination -- use `after_rid` with the last RID from the previous page)

```
Before fetching, tell me how many rows match Status=released in isa:Dataset on localhost catalog 1
```

(preflight count -- returns count only, does not fetch rows)

**Inserting rows:**

```
Insert a new dataset into isa:Dataset in catalog 1 on localhost with Title="Smoke Test" and Description="Created via MCP"
```

```
Add two new terms to the vocab:Species vocabulary in catalog 2 on localhost: "Mus musculus" and "Rattus norvegicus"
```

**Updating rows:**

```
Update the dataset with RID 1-ABCD in isa:Dataset on localhost catalog 1 -- set its Description to "Updated via MCP"
```

(only the Description column is written; other columns are unchanged)

```
Set the Status of all datasets with Status=draft and RID in [1-AAAA, 1-BBBB] to "in review"
```

**Deleting rows:**

```
Delete the dataset with RID 1-ABCD from isa:Dataset in catalog 1 on localhost
```

---

## Queries

**Attribute queries:**

```
Query isa:Dataset/Status=released in catalog 1 on localhost and return Title, RID, and Release_Date
```

```
Query the path isa:Dataset/isa:Dataset_Experiment/isa:Experiment in catalog 1 on localhost
```

**Aggregate queries:**

```
Count how many datasets are in catalog 1 on localhost grouped by Status
```

```
What is the total size in bytes of all files in isa:File grouped by File_Type in catalog 1 on localhost?
```

**Count shorthand:**

```
How many rows are in isa:Experiment in catalog 1 on localhost where Status=released?
```

---

## Annotations

**Inspecting annotations:**

```
Show me the visible-columns annotation for isa:Dataset in catalog 1 on localhost
```

```
What are the foreign keys defined on isa:Experiment in catalog 1 on localhost?
```

```
Show me all annotations on the RID column of isa:Dataset in catalog 1 on localhost
```

**Display names:**

```
Set the display name for the isa:Dataset table in catalog 1 on localhost to "Research Dataset"
```

```
Set the display name for the isa:Dataset:Title column in catalog 1 on localhost to "Dataset Title"
```

**Visible columns:**

```
Add the isa:Dataset:Release_Date column to the visible-columns for the compact context in catalog 1 on localhost
```

```
Remove the isa:Dataset:Internal_Notes column from visible-columns for all contexts in catalog 1 on localhost
```

```
Move Title to the top of the visible-columns list for isa:Dataset in the entry context on localhost catalog 1
```

**Row name patterns:**

```
Set the row name for isa:Dataset in catalog 1 on localhost to the Handlebars template "{{Title}} ({{RID}})"
```

```
Preview the template "{{Title}} - {{Status}}" using sample data from isa:Dataset in catalog 1 on localhost
```

**Catalog-level Chaise display:**

```
Set the navbar brand text for catalog 1 on localhost to "My Research Portal" and the page title to "My Portal"
```

```
Build a navbar menu automatically from the schemas in catalog 1 on localhost and apply it
```

---

## Schema DDL

```
Create a table called Sample in the isa schema of catalog 1 on localhost with columns Name (text, required) and Description (text, optional)
```

```
Add a column called Batch_ID of type text to isa:Experiment in catalog 1 on localhost
```

```
Set the description of isa:Dataset in catalog 1 on localhost to "Top-level record for a research dataset"
```

```
Make the isa:Experiment:Notes column nullable in catalog 1 on localhost
```

---

## Vocabulary

```
List all terms in the vocab:Data_Type vocabulary in catalog 1 on localhost
```

```
Look up the term "RNA-seq" in vocab:Assay_Type in catalog 1 on localhost
```

```
Add a term called "Single-cell RNA-seq" to vocab:Assay_Type in catalog 1 on localhost
```

```
Add "scRNA-seq" as a synonym for the term "Single-cell RNA-seq" in vocab:Assay_Type on localhost catalog 1
```

```
Create a new vocabulary table called Sample_Type in the vocab schema of catalog 1 on localhost
```

---

## Catalog Administration

```
Create a new catalog on localhost
```

```
Create a clone of catalog 1 on localhost
```

```
Clone catalog 1 on localhost to a new catalog, copying data and annotations but not ACL policy
```

```
Clone catalog 1 on localhost as a background task -- I'll check back when it's done
```

(use `clone_catalog_async`; poll with the task ID returned)

```
Create an alias "my-project" pointing to catalog 3 on localhost
```

```
Generate a citation URL for RID 1-ABCD in catalog 1 on localhost
```

---

## Background Tasks

```
What background tasks are currently running?
```

```
Check the status of task a1b2c3d4-...
```

```
Show me all completed tasks
```

```
Cancel task a1b2c3d4-...
```

---

## Hatrac Object Store

```
List the contents of the /hatrac/isa namespace on localhost
```

```
Show me the metadata for the object at /hatrac/isa/data/file1.csv on localhost
```

```
Create the namespace /hatrac/isa/staging on localhost
```

---

## RAG

**Search:**

```
Search for documentation about ERMrest foreign key path syntax
```

```
What does the deriva-py documentation say about uploading files to Hatrac?
```

```
Search the schema for catalog 1 on localhost for tables related to experiments
```

(schema search is scoped to the current user's catalog view)

**Index management:**

```
What is the current state of the documentation index?
```

```
Incrementally update the ermrest-docs documentation index
```

```
Force a full re-index of all documentation sources
```

```
Reindex the schema for catalog 1 on localhost
```

**Source management:**

```
Add a documentation source called "my-tool-docs" from github.com/my-org/my-tool, branch main, docs/ folder
```

```
Remove the my-tool-docs documentation source
```