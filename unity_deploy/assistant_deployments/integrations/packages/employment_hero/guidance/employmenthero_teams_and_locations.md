# Teams and Locations

For UK property-management clients, **EH locations correspond to
managed properties or portfolios**.  This is the geographic layer of
the workforce graph.

## Locations

```python
await list_locations(mock=False)
await get_location("loc-battersea", mock=False)
await query_local_locations(mock=False)
```

The synced `EmploymentHero/Locations` carries `name`,
`address_line_1/2`, `city`, `postcode`, `country`.

## Teams

Teams group employees by function (Property Management, Maintenance,
Customer Contact, etc.).  Each team has a `manager_id` and an optional
`location_id`.

```python
await list_teams(mock=False)
await list_team_members("team-maintenance-camden", mock=False)
await query_local_teams(location_id="loc-camden", mock=False)
```

`EmploymentHero/Teams/Memberships` is the (team_id, employee_id)
junction table.

## Common cross-record questions

```sql
-- Who works at Battersea Portfolio?
SELECT e.first_name, e.last_name, e.position, t.name AS team
FROM   EmploymentHero/Employees e
JOIN   EmploymentHero/Teams t ON e.team_id = t.id
WHERE  e.location_id = 'loc-battersea'
   AND e.status = 'active';
```

Use `dm.filter_join(...)` to express this against the synced copy.
